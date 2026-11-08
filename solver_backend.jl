#!/usr/bin/env julia

# SmartGate session allocator.
# Initial mode: one joint JuMP/Gurobi ILP over all parking sessions.
# Repair mode: preserve unaffected bookings and greedily reassign only changed
# sessions, minimizing unnecessary gate moves.
#
# Aircraft and stand width classes are supplied by Python after reading the
# repository's static JSON references. The same-or-next-larger rule is a
# simulator heuristic only, NOT verified Pearson/GTAA operational approval.

using JSON
using JuMP
using Gurobi

const WIDTH_CLASSES = ["A", "B", "C", "D", "E", "F"]
const UNASSIGNED_PENALTY = 1.0e7
const PRIORITY_UNASSIGNED_PENALTY = 1.0e9
const NEXT_LARGER_CLASS_PENALTY = 500.0
const TURNBACK_REMOTE_PENALTY = 5_000.0
const EMERGENCY_GATE_PENALTY = 1_000.0
const SOLVER_TIME_LIMIT = 6.0

field(obj, key, default=nothing) = obj isa AbstractDict ? get(obj, key, default) : default
as_string(x, default="") = x === nothing ? default : string(x)
as_bool(x) = x isa Bool ? x : (x === nothing ? false : lowercase(string(x)) in ("true", "1", "yes"))

function intervals_overlap(a_start::Float64, a_end::Float64, b_start::Float64, b_end::Float64)
    return a_start < b_end && b_start < a_end
end

function width_compatible(aircraft_class, stand_class)
    aircraft_class in WIDTH_CLASSES || return false
    stand_class in WIDTH_CLASSES || return false
    ai = findfirst(==(aircraft_class), WIDTH_CLASSES)
    gi = findfirst(==(stand_class), WIDTH_CLASSES)
    return gi == ai || gi == ai + 1
end

function overlaps_outage(session, gate_id, outages)
    windows = field(outages, gate_id, Any[])
    s = Float64(field(session, "start", 0.0))
    e = Float64(field(session, "end", 0.0))
    for window in windows
        length(window) == 2 || continue
        if intervals_overlap(s, e, Float64(window[1]), Float64(window[2]))
            return true
        end
    end
    return false
end

function gate_cost(session, gate, index::Int)
    ac_class = as_string(field(session, "widthClass", nothing))
    gate_class = as_string(field(gate, "widthClass", nothing))
    cost = ac_class == gate_class ? 0.0 : NEXT_LARGER_CLASS_PENALTY
    kind = as_string(field(gate, "kind", "gate"))
    remote_allowed = as_bool(field(session, "remoteAllowed", false))
    turnback = as_bool(field(session, "turnback", false))
    if kind == "remote"
        cost += turnback ? TURNBACK_REMOTE_PENALTY : 0.0
    elseif remote_allowed && !turnback
        # For non-turnback emergencies, prefer a remote position where feasible
        # so ordinary passenger gates remain available.
        cost += EMERGENCY_GATE_PENALTY
    end
    # Stable deterministic tie-break without materially changing the objective.
    return cost + index * 0.001
end

function candidate_gates(session, gates, outages)
    result = Tuple{String,Int}[]
    ac_class = as_string(field(session, "widthClass", nothing))
    remote_allowed = as_bool(field(session, "remoteAllowed", false))
    for (index, gate) in enumerate(gates)
        gate_id = as_string(field(gate, "id", ""))
        isempty(gate_id) && continue
        kind = as_string(field(gate, "kind", "gate"))
        kind == "remote" && !remote_allowed && continue
        gate_class = as_string(field(gate, "widthClass", nothing))
        width_compatible(ac_class, gate_class) || continue
        overlaps_outage(session, gate_id, outages) && continue
        push!(result, (gate_id, index))
    end
    return result
end

function solve_initial(payload)
    sessions = field(payload, "sessions", Any[])
    gates = field(payload, "gates", Any[])
    outages = field(payload, "outages", Dict{String,Any}())
    n = length(sessions)
    if n == 0
        JSON.print(stdout, Dict("assignments" => Any[], "unassigned" => Any[]))
        return
    end

    session_ids = [as_string(field(s, "id", "")) for s in sessions]
    length(unique(session_ids)) == length(session_ids) || error("Duplicate parking-session IDs.")
    candidates = Dict{Int,Vector{Tuple{String,Int}}}()
    for i in 1:n
        candidates[i] = candidate_gates(sessions[i], gates, outages)
    end

    model = Model(Gurobi.Optimizer)
    set_optimizer_attribute(model, "OutputFlag", 0)
    set_optimizer_attribute(model, "TimeLimit", SOLVER_TIME_LIMIT)
    set_optimizer_attribute(model, "MIPGap", 0.02)
    set_optimizer_attribute(model, "Threads", 1)

    x = Dict{Tuple{Int,String},VariableRef}()
    unassigned = Dict{Int,VariableRef}()
    for i in 1:n
        expr = AffExpr(0.0)
        for (gate_id, _) in candidates[i]
            x[(i, gate_id)] = @variable(model, binary=true)
            add_to_expression!(expr, 1.0, x[(i, gate_id)])
        end
        unassigned[i] = @variable(model, binary=true)
        add_to_expression!(expr, 1.0, unassigned[i])
        @constraint(model, expr == 1.0)
    end

    # A physical position cannot host overlapping parking sessions.
    gate_ids = String[]
    for i in 1:n, (gate_id, _) in candidates[i]
        gate_id in gate_ids || push!(gate_ids, gate_id)
    end
    for gate_id in gate_ids
        possible = [i for i in 1:n if any(c[1] == gate_id for c in candidates[i])]
        for a in 1:(length(possible) - 1)
            for b in (a + 1):length(possible)
                i, j = possible[a], possible[b]
                si, sj = sessions[i], sessions[j]
                if intervals_overlap(
                    Float64(field(si, "start", 0.0)), Float64(field(si, "end", 0.0)),
                    Float64(field(sj, "start", 0.0)), Float64(field(sj, "end", 0.0))
                )
                    @constraint(model, x[(i, gate_id)] + x[(j, gate_id)] <= 1)
                end
            end
        end
    end

    objective = AffExpr(0.0)
    for i in 1:n
        priority = as_bool(field(sessions[i], "priority", false))
        penalty = priority ? PRIORITY_UNASSIGNED_PENALTY : UNASSIGNED_PENALTY
        add_to_expression!(objective, penalty, unassigned[i])
        for (gate_id, index) in candidates[i]
            gate = gates[index]
            add_to_expression!(objective, gate_cost(sessions[i], gate, index), x[(i, gate_id)])
        end
    end
    @objective(model, Min, objective)
    optimize!(model)

    has_values(model) || error("Gurobi returned no feasible solution: $(termination_status(model))")
    assignments = Vector{Vector{String}}()
    unassigned_ids = String[]
    for i in 1:n
        selected = nothing
        for (gate_id, _) in candidates[i]
            if value(x[(i, gate_id)]) > 0.5
                selected = gate_id
                break
            end
        end
        if selected === nothing
            push!(unassigned_ids, session_ids[i])
        else
            push!(assignments, [session_ids[i], selected])
        end
    end
    JSON.print(stdout, Dict(
        "assignments" => assignments,
        "unassigned" => unassigned_ids,
        "objective" => objective_value(model),
        "termination_status" => string(termination_status(model)),
    ))
end

function solve_repair(payload)
    sessions = field(payload, "sessions", Any[])
    gates = field(payload, "gates", Any[])
    outages = field(payload, "outages", Dict{String,Any}())
    old_assignments_raw = field(payload, "assignments", Dict{String,Any}())
    changed_raw = field(payload, "changed", Any[])
    changed = Set(as_string(x) for x in changed_raw)
    by_id = Dict(as_string(field(s, "id", "")) => s for s in sessions)
    old_assignments = Dict{String,String}(as_string(k) => as_string(v) for (k, v) in old_assignments_raw)

    # Fixed bookings are every still-valid assignment outside the repair set.
    occupied = Dict{String,Vector{Tuple{Float64,Float64}}}()
    for (sid, gate_id) in old_assignments
        sid in changed && continue
        haskey(by_id, sid) || continue
        s = by_id[sid]
        push!(get!(occupied, gate_id, Tuple{Float64,Float64}[]),
              (Float64(field(s, "start", 0.0)), Float64(field(s, "end", 0.0))))
    end

    decision = [sid for sid in changed if haskey(by_id, sid)]
    candidate_map = Dict{String,Vector{Tuple{String,Int}}}()
    for sid in decision
        candidate_map[sid] = candidate_gates(by_id[sid], gates, outages)
    end
    sort!(decision, by = sid -> (
        as_bool(field(by_id[sid], "priority", false)) ? 0 : 1,
        length(candidate_map[sid]),
        haskey(old_assignments, sid) ? 0 : 1,
        sid,
    ))

    assignments = Vector{Vector{String}}()
    reassignments = Vector{Vector{String}}()
    unassigned = String[]
    for sid in decision
        session = by_id[sid]
        old_gate = get(old_assignments, sid, nothing)
        candidates = copy(candidate_map[sid])
        sort!(candidates, by = item -> (
            old_gate !== nothing && item[1] == old_gate ? 0 : 1,
            gate_cost(session, gates[item[2]], item[2]),
            item[1],
        ))
        selected = nothing
        s_start = Float64(field(session, "start", 0.0))
        s_end = Float64(field(session, "end", 0.0))
        for (gate_id, _) in candidates
            conflicts = any(
                intervals_overlap(s_start, s_end, booking[1], booking[2])
                for booking in get(occupied, gate_id, Tuple{Float64,Float64}[])
            )
            conflicts && continue
            selected = gate_id
            push!(get!(occupied, gate_id, Tuple{Float64,Float64}[]), (s_start, s_end))
            break
        end
        if selected === nothing
            push!(unassigned, sid)
        elseif old_gate === nothing
            push!(assignments, [sid, selected])
        elseif selected != old_gate
            push!(reassignments, [sid, selected])
        end
    end
    JSON.print(stdout, Dict(
        "assignments" => assignments,
        "reassignments" => reassignments,
        "unassigned" => unassigned,
    ))
end

function main()
    input_text = read(stdin, String)
    isempty(strip(input_text)) && error("No solver payload received on stdin.")
    payload = JSON.parse(input_text)
    mode = as_string(field(payload, "mode", "initial"))
    if mode == "initial"
        solve_initial(payload)
    elseif mode == "repair"
        solve_repair(payload)
    else
        error("Unknown solver mode: $mode")
    end
end

try
    main()
catch err
    showerror(stderr, err, catch_backtrace())
    println(stderr)
    exit(1)
end
