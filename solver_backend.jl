#!/usr/bin/env julia

# JuMP + Gurobi backend for the GMS evaluator.
#
# Input: one JSON observation on stdin (the evaluator's decide() observation).
# Output: one JSON object on stdout:
#   {"assignments":[[flight_id,gate_id],...],
#    "reassignments":[[flight_id,gate_id],...]}
#
# Existing, unchanged assignments are treated as fixed occupancy. Waiting
# flights and changed flights are optimized jointly. Changed flights are
# removed from their old occupancy before optimization, then either retained
# at their current gate or moved if necessary / worthwhile.

using JSON
using JuMP
using Gurobi

const CARGO = 0
const DOMESTIC = 1
const INTERNATIONAL = 2

# These penalties reflect the evaluator's priorities: avoid unassigned
# flights first, then avoid unnecessary moves and premium-gate use.
const UNASSIGNED_PENALTY = 100_000.0
const EMERGENCY_UNASSIGNED_PENALTY = 10_000_000.0
const REASSIGNMENT_PENALTY = 1_000.0
const DOMESTIC_ON_INTERNATIONAL_PENALTY = 1_000.0

function field(obj, key, default=nothing)
    obj isa AbstractDict || return default
    return get(obj, key, default)
end

function as_float(x, label)
    x === nothing && error("Missing required numeric field: $label")
    return Float64(x)
end

function as_int(x, label)
    x === nothing && error("Missing required integer field: $label")
    return Int(x)
end

function as_bool(x)
    x === nothing && return false
    x isa Bool && return x
    x isa Number && return x != 0
    s = lowercase(strip(string(x)))
    return s in ("true", "1", "yes", "y")
end

function intervals_of(flight, label)
    raw = field(flight, "intervals", nothing)
    raw === nothing && error("Flight $label has no 'intervals' field.")
    intervals = Tuple{Float64,Float64}[]
    for item in raw
        length(item) == 2 || error("Invalid interval for flight $label: $item")
        a, b = Float64(item[1]), Float64(item[2])
        b > a || error("Non-positive occupancy interval for flight $label: $item")
        push!(intervals, (a, b))
    end
    return intervals
end

overlap(a, b) = a[1] < b[2] && b[1] < a[2]

function overlaps_any(a_intervals, b_intervals)
    for a in a_intervals, b in b_intervals
        overlap(a, b) && return true
    end
    return false
end

function compatible(flight, gate)
    wingspan = as_float(field(flight, "wingspan", nothing), "flight.wingspan")
    max_wingspan = as_float(field(gate, "max_wingspan", nothing), "gate.max_wingspan")
    wingspan <= max_wingspan || return false

    if as_bool(field(flight, "jetbridge_required", false)) &&
       !as_bool(field(gate, "jetbridge", false))
        return false
    end

    category = as_int(field(flight, "category", nothing), "flight.category")
    gate_type = as_int(field(gate, "gate_type", nothing), "gate.gate_type")

    if category == CARGO
        return gate_type == CARGO
    elseif category == INTERNATIONAL
        return gate_type == INTERNATIONAL
    elseif category == DOMESTIC
        return gate_type in (DOMESTIC, INTERNATIONAL)
    end

    error("Unknown flight category $category (expected 0, 1, or 2).")
end

function gate_cost(flight, gate)
    cost = as_float(field(gate, "dist", 0.0), "gate.dist")

    category = as_int(field(flight, "category", nothing), "flight.category")
    gate_type = as_int(field(gate, "gate_type", nothing), "gate.gate_type")

    if category == DOMESTIC && gate_type == INTERNATIONAL
        cost += DOMESTIC_ON_INTERNATIONAL_PENALTY
    end

    return cost
end

function booking_flight_id(booking)
    return string(field(booking, "flight_id", ""))
end

function main()
    input_text = read(stdin, String)
    observation = JSON.parse(input_text)

    gates = field(observation, "gates", Dict{String,Any}())
    outages = field(observation, "gate_outages", Dict{String,Any}())
    waiting = field(observation, "waiting_flights", Dict{String,Any}())
    changed = field(observation, "changed_flights", Dict{String,Any}())
    gate_assignments = field(observation, "gate_assignments", Dict{String,Any}())

    # Build the decision set: all waiting flights plus all changed flights.
    # A changed flight uses its NEW flight view and remembers its current gate.
    flights = Dict{String,Dict{String,Any}}()
    old_gate = Dict{String,Union{Nothing,String}}()
    is_waiting = Set{String}()

    for (fid_raw, view) in waiting
        fid = string(fid_raw)
        flights[fid] = Dict{String,Any}(string(k) => v for (k, v) in view)
        old_gate[fid] = nothing
        push!(is_waiting, fid)
    end

    for (fid_raw, change) in changed
        fid = string(fid_raw)
        new_view = field(change, "new", nothing)
        new_view isa AbstractDict ||
            error("changed_flights[$fid] is missing its 'new' flight view.")

        flights[fid] = Dict{String,Any}(string(k) => v for (k, v) in new_view)
        current = field(change, "gate_id", nothing)
        old_gate[fid] = current === nothing ? nothing : string(current)
    end

    decision_ids = collect(keys(flights))
    sort!(decision_ids)

    # Fixed occupancy consists of bookings belonging to flights outside the
    # decision set. Changed flights' old bookings are intentionally removed.
    fixed_by_gate = Dict{String,Vector{Vector{Tuple{Float64,Float64}}}}()
    for (gid_raw, bookings) in gate_assignments
        gid = string(gid_raw)
        fixed_by_gate[gid] = Vector{Vector{Tuple{Float64,Float64}}}()
        for booking in bookings
            fid = booking_flight_id(booking)
            fid in decision_ids && continue
            ints = Tuple{Float64,Float64}[]
            for item in field(booking, "intervals", [])
                push!(ints, (Float64(item[1]), Float64(item[2])))
            end
            push!(fixed_by_gate[gid], ints)
        end
    end

    # Normalize outage intervals.
    outage_by_gate = Dict{String,Vector{Tuple{Float64,Float64}}}()
    for (gid_raw, windows) in outages
        gid = string(gid_raw)
        outage_by_gate[gid] = Tuple{Float64,Float64}[
            (Float64(w[1]), Float64(w[2])) for w in windows
        ]
    end

    # Candidate gates satisfy aircraft constraints, outage constraints, and
    # do not conflict with any unchanged booking already occupying the gate.
    gate_ids = sort!(string.(collect(keys(gates))))
    candidates = Dict{String,Vector{String}}()
    flight_intervals = Dict{String,Vector{Tuple{Float64,Float64}}}()

    for fid in decision_ids
        flight = flights[fid]
        flight_intervals[fid] = intervals_of(flight, fid)
        candidates[fid] = String[]

        for gid in gate_ids
            gate = gates[gid]
            compatible(flight, gate) || continue

            down_windows = get(outage_by_gate, gid, Tuple{Float64,Float64}[])
            overlaps_any(flight_intervals[fid], down_windows) && continue

            fixed_bookings = get(fixed_by_gate, gid, Vector{Vector{Tuple{Float64,Float64}}}())
            any(book -> overlaps_any(flight_intervals[fid], book), fixed_bookings) && continue

            push!(candidates[fid], gid)
        end
    end

    # Empty decision set is a valid no-op tick.
    if isempty(decision_ids)
        JSON.print(stdout, Dict("assignments" => Any[], "reassignments" => Any[]))
        return
    end

    model = Model(Gurobi.Optimizer)
    set_optimizer_attribute(model, "OutputFlag", 0)
    set_optimizer_attribute(model, "TimeLimit", 8.0)

    x = Dict{Tuple{String,String},VariableRef}()
    u = Dict{String,VariableRef}()

    for fid in decision_ids
        for gid in candidates[fid]
            x[(fid, gid)] = @variable(model, binary=true)
        end
        u[fid] = @variable(model, binary=true)

        assign_expr = AffExpr(0.0)
        for gid in candidates[fid]
            add_to_expression!(assign_expr, 1.0, x[(fid, gid)])
        end
        @constraint(model, assign_expr + u[fid] == 1)
    end

    # Decision flights cannot overlap one another on the same gate.
    for gid in gate_ids
        possible = [fid for fid in decision_ids if gid in candidates[fid]]
        for i in 1:length(possible)
            for j in (i + 1):length(possible)
                f1, f2 = possible[i], possible[j]
                if overlaps_any(flight_intervals[f1], flight_intervals[f2])
                    @constraint(model, x[(f1, gid)] + x[(f2, gid)] <= 1)
                end
            end
        end
    end

    # Objective: assign flights, avoid moves, avoid premium-gate use, and
    # prefer shorter walking distances.
    objective = AffExpr(0.0)

    for fid in decision_ids
        flight = flights[fid]
        priority = as_bool(field(flight, "priority", false))
        unassigned_penalty = priority ?
            EMERGENCY_UNASSIGNED_PENALTY : UNASSIGNED_PENALTY
        add_to_expression!(objective, unassigned_penalty, u[fid])

        for gid in candidates[fid]
            cost = gate_cost(flight, gates[gid])
            previous_gate = old_gate[fid]
            if previous_gate !== nothing && gid != previous_gate
                cost += REASSIGNMENT_PENALTY
            end
            add_to_expression!(objective, cost, x[(fid, gid)])
        end
    end

    @objective(model, Min, objective)
    optimize!(model)

    if !has_values(model)
        error("Gurobi returned no feasible solution (status: $(termination_status(model))).")
    end

    assignments = Vector{Vector{String}}()
    reassignments = Vector{Vector{String}}()

    for fid in decision_ids
        chosen_gate = nothing
        for gid in candidates[fid]
            if value(x[(fid, gid)]) > 0.5
                chosen_gate = gid
                break
            end
        end

        chosen_gate === nothing && continue

        if old_gate[fid] === nothing
            push!(assignments, [fid, chosen_gate])
        elseif chosen_gate != old_gate[fid]
            push!(reassignments, [fid, chosen_gate])
        end
        # A changed flight that remains at its existing gate needs no action.
    end

    JSON.print(stdout, Dict(
        "assignments" => assignments,
        "reassignments" => reassignments
    ))
end

try
    main()
catch err
    # Keep stdout reserved for machine-readable JSON.
    showerror(stderr, err, catch_backtrace())
    println(stderr)
    exit(1)
end
