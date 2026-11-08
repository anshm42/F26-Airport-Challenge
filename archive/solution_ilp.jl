
using JSON
using JuMP
using Gurobi
using Dates

# ============================================================
# CONFIGURATION
# ============================================================

const SCENARIO_DATE = Date(2025, 6, 15)
const DEFAULT_GROUND_MINUTES = 90

const SOLVER_TIME_LIMIT = 5.0

const UNASSIGNED_PENALTY = 100_000.0
const EMERGENCY_UNASSIGNED_PENALTY = 10_000_000.0
const REASSIGNMENT_PENALTY = 1_000.0


# ============================================================
# GENERAL HELPER FUNCTIONS
# ============================================================

function getvalue(obj, key, default=nothing)
    if obj isa AbstractDict
        return get(obj, key, default)
    end
    return default
end


function safe_string(value, default="")
    if value === nothing || ismissing(value)
        return default
    end
    return string(value)
end


function safe_bool(value)
    if value isa Bool
        return value
    elseif value isa Number
        return value != 0
    elseif value isa AbstractString
        return lowercase(strip(value)) in ("true", "1", "yes")
    end
    return false
end


function flight_id(flight)
    carrier = safe_string(getvalue(flight, "carrier", ""))
    number = safe_string(getvalue(flight, "flightNumber", ""))
    suffix = safe_string(getvalue(flight, "flightSuffix", ""))
    date = safe_string(
        getvalue(flight, "originatingDate", string(SCENARIO_DATE))
    )

    return carrier * "-" * number * "-" * suffix * "-" * date
end


function parse_clock(value, date=SCENARIO_DATE)

    value === nothing && error("Missing time value")

    s = strip(string(value))

    formats = [
        dateformat"yyyy-mm-ddTHH:MM:SS",
        dateformat"yyyy-mm-ddTHH:MM",
        dateformat"yyyy-mm-dd HH:MM:SS",
        dateformat"yyyy-mm-dd HH:MM",
        dateformat"HH:MM:SS",
        dateformat"HH:MM"
    ]

    for fmt in formats
        try
            parsed = DateTime(s, fmt)

            if occursin("-", s)
                return parsed
            else
                return DateTime(date) +
                       Hour(hour(parsed)) +
                       Minute(minute(parsed)) +
                       Second(second(parsed))
            end
        catch
        end
    end

    error("Unrecognized time format: $s")
end


function intervals_overlap(a_start, a_end, b_start, b_end)
    # Half-open intervals: [start, end)
    return a_start < b_end && b_start < a_end
end


# ============================================================
# STATIC INFORMATION
# ============================================================

function load_static_info(filename)

    if !isfile(filename)
        error("Static information file not found: $filename")
    end

    static_info = JSON.parse(read(filename, String))

    for key in ("gate_info", "aircraft_info", "stations")
        if !haskey(static_info, key)
            error("Missing '$key' section in $filename")
        end
    end

    return static_info
end


function get_gates(static_info)

    gate_info = getvalue(
        static_info,
        "gate_info",
        Dict()
    )

    return Dict{String, Any}(
        string(gate_id) => gate
        for (gate_id, gate) in gate_info
    )
end


# ============================================================
# FLIGHT INFORMATION
# ============================================================

function get_aircraft_type(flight)

    raw = flight["raw"]
    legs = getvalue(raw, "legs", [])

    for leg in legs

        aircraft_type = getvalue(
            leg,
            "aircraftType",
            nothing
        )

        if aircraft_type !== nothing
            return uppercase(strip(string(aircraft_type)))
        end
    end

    return "UNKNOWN"
end


function get_aircraft_specs(flight, static_info)

    aircraft_type = get_aircraft_type(flight)

    aircraft_info = static_info["aircraft_info"]

    if !haskey(aircraft_info, aircraft_type)
        return nothing
    end

    return aircraft_info[aircraft_type]
end


# ============================================================
# FLIGHT OCCUPANCY
# ============================================================

function get_yyz_occupancy(flight)

    legs = getvalue(flight, "legs", [])

    arrival_times = DateTime[]
    departure_times = DateTime[]

    flight_date_string = safe_string(
        getvalue(
            flight,
            "originatingDate",
            string(SCENARIO_DATE)
        )
    )

    flight_date = try
        Date(flight_date_string, dateformat"yyyy-mm-dd")
    catch
        SCENARIO_DATE
    end

    for leg in legs

        arrival_station = safe_string(
            getvalue(leg, "arrivalStation", "")
        )

        departure_station = safe_string(
            getvalue(leg, "departureStation", "")
        )

        arrival_time = getvalue(
            leg,
            "scheduledArrivalTime",
            nothing
        )

        departure_time = getvalue(
            leg,
            "scheduledDepartureTime",
            nothing
        )

        if arrival_station == "YYZ" && arrival_time !== nothing
            push!(
                arrival_times,
                parse_clock(arrival_time, flight_date)
            )
        end

        if departure_station == "YYZ" && departure_time !== nothing
            push!(
                departure_times,
                parse_clock(departure_time, flight_date)
            )
        end
    end

    if !isempty(arrival_times) && !isempty(departure_times)

        start_time = minimum(arrival_times)
        end_time = maximum(departure_times)

    elseif !isempty(arrival_times)

        start_time = minimum(arrival_times)
        end_time = start_time + Minute(DEFAULT_GROUND_MINUTES)

    elseif !isempty(departure_times)

        end_time = maximum(departure_times)
        start_time = end_time - Minute(DEFAULT_GROUND_MINUTES)

    else
        error(
            "Flight $(flight_id(flight)) has no YYZ arrival or departure"
        )
    end

    if end_time <= start_time
        error("Invalid occupancy interval for $(flight_id(flight))")
    end

    return start_time, end_time
end


# ============================================================
# EVENT PROCESSING
# ============================================================

function get_event_type(event)
    return safe_string(getvalue(event, "action", ""))
end


function get_event_flight(event)

    nested = getvalue(event, "flight", nothing)

    if nested isa AbstractDict
        return nested
    end

    return event
end


function add_gate_outage!(event, outages)

    gate_id = safe_string(
        getvalue(
            event,
            "gate",
            getvalue(event, "gateId", "")
        )
    )

    start_value = getvalue(
        event,
        "scheduledStartTime",
        nothing
    )

    end_value = getvalue(
        event,
        "scheduledEndTime",
        nothing
    )

    if isempty(gate_id) ||
       start_value === nothing ||
       end_value === nothing

        println("Warning: Ignoring incomplete gate outage event.")
        return
    end

    start_time = parse_clock(start_value, SCENARIO_DATE)
    end_time = parse_clock(end_value, SCENARIO_DATE)

    push!(
        outages,
        Dict(
            "gate" => gate_id,
            "start" => start_time,
            "end" => end_time
        )
    )

    println(
        "Gate outage recorded: $gate_id from $start_time to $end_time"
    )
end


function apply_event!(event, flights, outages, old_assignments)

    event_type = get_event_type(event)

    if event_type == "GateOutage"
        add_gate_outage!(event, outages)
        return
    end

    flight = get_event_flight(event)
    fid = flight_id(flight)

    # --------------------------------------------------------
    # Cancellation
    # --------------------------------------------------------

    if event_type == "CancelFlight"

        if haskey(flights, fid)
            delete!(flights, fid)
            delete!(old_assignments, fid)

            println("Cancelled flight: $fid")
        end

        return
    end

    # --------------------------------------------------------
    # Flight creation, timing update, or emergency flight
    # --------------------------------------------------------

    if event_type in (
        "CreateFlight",
        "UpdateTiming",
        "DivertFlight"
    )

        try

            # Some events have legs alongside the nested flight.
            # Merge them before storing the flight.

            if isempty(getvalue(flight, "legs", []))

                flight = merge(
                    Dict(flight),
                    Dict(
                        "legs" => getvalue(event, "legs", [])
                    )
                )
            end

            start_time, end_time = get_yyz_occupancy(flight)

            previous = get(
                flights,
                fid,
                nothing
            )

            old_gate = if previous === nothing
                get(old_assignments, fid, nothing)
            else
                previous["old_gate"]
            end

            is_priority = event_type == "DivertFlight"

            if previous !== nothing
                is_priority = is_priority || previous["priority"]
            end

            flights[fid] = Dict(
                "id" => fid,
                "raw" => flight,
                "start" => start_time,
                "end" => end_time,
                "date" => Date(start_time),
                "priority" => is_priority,
                "old_gate" => old_gate,
                "reason" => safe_string(
                    getvalue(event, "reason", "")
                )
            )

            if event_type == "DivertFlight"

                println("Emergency flight added: $fid")

            elseif event_type == "CreateFlight"

                println("New flight added: $fid")

            else

                println("Flight updated: $fid")

            end

        catch err

            println(
                "Warning: Could not process $event_type event: $err"
            )

        end

        return
    end

    println("Warning: Unrecognized event type: $event_type")
end


# ============================================================
# GATE COMPATIBILITY
# ============================================================

function compatible_aircraft(flight, gate, static_info)

    # --------------------------------------------------------
    # Get aircraft specifications from static.json
    # --------------------------------------------------------

    aircraft_type = get_aircraft_type(flight)

    aircraft_info = static_info["aircraft_info"]

    if !haskey(aircraft_info, aircraft_type)

        println(
            "WARNING: Unknown aircraft type: $aircraft_type " *
            "for $(flight["id"])"
        )

        return false
    end

    aircraft = aircraft_info[aircraft_type]

    wingspan = Float64(
        getvalue(aircraft, "wingspan", 0.0)
    )

    requires_jetbridge = safe_bool(
        getvalue(aircraft, "jetbridge_required", false)
    )

    is_cargo = safe_bool(
        getvalue(aircraft, "cargo", false)
    )


    # --------------------------------------------------------
    # Get gate specifications from static.json
    # --------------------------------------------------------

    max_wingspan = Float64(
        getvalue(gate, "max_wingspan", 0.0)
    )

    has_jetbridge = safe_bool(
        getvalue(gate, "jetbridge", false)
    )

    gate_type = Int(
        getvalue(gate, "gate_type", -1)
    )


    # --------------------------------------------------------
    # Wingspan compatibility
    # --------------------------------------------------------

    if wingspan > max_wingspan
        return false
    end


    # --------------------------------------------------------
    # Jetbridge compatibility
    # --------------------------------------------------------

    if requires_jetbridge && !has_jetbridge
        return false
    end


    # --------------------------------------------------------
    # Cargo compatibility
    #
    # Gate type 0 = cargo
    # Gate type 1 = domestic
    # Gate type 2 = international
    # --------------------------------------------------------

    if is_cargo && gate_type != 0
        return false
    end

    if !is_cargo && gate_type == 0
        return false
    end


    # --------------------------------------------------------
    # Domestic / international compatibility
    # --------------------------------------------------------

    if !is_cargo

        stations = static_info["stations"]

        raw = flight["raw"]

        is_international = false

        for leg in getvalue(raw, "legs", [])

            departure = safe_string(
                getvalue(leg, "departureStation", "")
            )

            arrival = safe_string(
                getvalue(leg, "arrivalStation", "")
            )

            # Determine the other station in the YYZ leg.

            other_station = if departure == "YYZ"
                arrival
            elseif arrival == "YYZ"
                departure
            else
                ""
            end

            station_type = lowercase(
                safe_string(
                    getvalue(stations, other_station, "")
                )
            )

            if station_type == "international"
                is_international = true
                break
            end
        end

        # International flights must use international gates.
        # Domestic flights may use domestic or international gates.

        if is_international && gate_type != 2
            return false
        end

    end

    return true
end


# ============================================================
# GATE OUTAGE CHECKING
# ============================================================

function gate_available(gate_id, flight, outages)

    for outage in outages

        if outage["gate"] == gate_id

            if intervals_overlap(
                flight["start"],
                flight["end"],
                outage["start"],
                outage["end"]
            )

                return false
            end
        end
    end

    return true
end


# ============================================================
# CANDIDATE GATE GENERATION
# ============================================================

function get_candidates(
    fid,
    flight,
    gates,
    outages,
    static_info
)

    candidates = String[]

    for (gate_id, gate) in gates

        if compatible_aircraft(
            flight,
            gate,
            static_info
        ) && gate_available(
            gate_id,
            flight,
            outages
        )

            push!(candidates, gate_id)

        end
    end

    return candidates
end


# ============================================================
# OBJECTIVE COST
# ============================================================

function gate_cost(flight, gate)

    # Shorter walking distance is preferred.
    return Float64(
        getvalue(gate, "dist", 0.0)
    )
end


# ============================================================
# OPTIMIZATION MODEL
# ============================================================

function optimize_gates(
    flights,
    gates,
    outages,
    static_info;
    time_limit=SOLVER_TIME_LIMIT
)

    flight_ids = collect(keys(flights))
    gate_ids = collect(keys(gates))

    candidates = Dict{String, Vector{String}}()

    # --------------------------------------------------------
    # Generate candidate gates for every flight
    # --------------------------------------------------------

    for fid in flight_ids

        candidates[fid] = get_candidates(
            fid,
            flights[fid],
            gates,
            outages,
            static_info
        )

    end

    # Print compatibility diagnostics.

    for fid in flight_ids

        flight = flights[fid]

        println("\nFlight: ", fid)
        println("Aircraft: ", get_aircraft_type(flight))
        println(
            "Occupancy: ",
            flight["start"],
            " -> ",
            flight["end"]
        )

        println("Compatible gates: ", candidates[fid])

        if isempty(candidates[fid])
            println("WARNING: No compatible gates for $fid")
        end

    end


    # --------------------------------------------------------
    # Create optimization model
    # --------------------------------------------------------

    model = Model(Gurobi.Optimizer)

    set_optimizer_attribute(model, "TimeLimit", time_limit)
    set_optimizer_attribute(model, "OutputFlag", 0)


    # x[f,g] = 1 if flight f is assigned to gate g.

    x = Dict{Tuple{String,String}, VariableRef}()

    for fid in flight_ids

        for gid in candidates[fid]

            x[(fid, gid)] = @variable(
                model,
                binary=true,
                base_name="x_$(fid)_$(gid)"
            )

        end
    end


    # u[f] = 1 if flight f is unassigned.

    u = Dict{String, VariableRef}()

    for fid in flight_ids

        u[fid] = @variable(
            model,
            binary=true,
            base_name="unassigned_$(fid)"
        )

    end


    # r[f] = 1 if flight f moves from its previous gate.

    r = Dict{String, VariableRef}()

    for fid in flight_ids

        r[fid] = @variable(
            model,
            binary=true,
            base_name="reassigned_$(fid)"
        )

    end


    # --------------------------------------------------------
    # Constraint 1:
    # Each flight gets one gate or remains unassigned.
    # --------------------------------------------------------

    for fid in flight_ids

        @constraint(
            model,
            sum(
                (x[(fid, gid)] for gid in candidates[fid]);
                init=0.0
            ) + u[fid] == 1
        )

    end


    # --------------------------------------------------------
    # Constraint 2:
    # No overlapping flights at the same gate.
    # --------------------------------------------------------

    for gid in gate_ids

        assigned_to_gate = [
            fid for fid in flight_ids
            if gid in candidates[fid]
        ]

        for i in 1:length(assigned_to_gate)

            for j in (i + 1):length(assigned_to_gate)

                f1 = assigned_to_gate[i]
                f2 = assigned_to_gate[j]

                if intervals_overlap(
                    flights[f1]["start"],
                    flights[f1]["end"],
                    flights[f2]["start"],
                    flights[f2]["end"]
                )

                    @constraint(
                        model,
                        x[(f1, gid)] + x[(f2, gid)] <= 1
                    )

                end
            end
        end
    end


    # --------------------------------------------------------
    # Constraint 3:
    # Count changes from previous assignments.
    # --------------------------------------------------------

    for fid in flight_ids

        old_gate = flights[fid]["old_gate"]

        if old_gate === nothing

            @constraint(model, r[fid] == 0)

        else

            old_gate_string = string(old_gate)

            assigned_expression = sum(
                (x[(fid, gid)] for gid in candidates[fid]);
                init=0.0
            )

            old_gate_expression = if old_gate_string in candidates[fid]
                x[(fid, old_gate_string)]
            else
                0.0
            end

            @constraint(
                model,
                r[fid] <= assigned_expression
            )

            @constraint(
                model,
                r[fid] >= assigned_expression - old_gate_expression
            )

        end
    end


    # --------------------------------------------------------
    # Objective function
    # --------------------------------------------------------

    unassigned_cost = sum(
        (
            (flights[fid]["priority"] ?
                EMERGENCY_UNASSIGNED_PENALTY :
                UNASSIGNED_PENALTY) * u[fid]
            for fid in flight_ids
        );
        init=0.0
    )

    reassignment_cost = REASSIGNMENT_PENALTY * sum(
        (r[fid] for fid in flight_ids);
        init=0.0
    )

    distance_cost = sum(
        (
            gate_cost(flights[fid], gates[gid]) * x[(fid, gid)]
            for fid in flight_ids
            for gid in candidates[fid]
        );
        init=0.0
    )

    @objective(
        model,
        Min,
        unassigned_cost +
        reassignment_cost +
        distance_cost
    )


    # --------------------------------------------------------
    # Solve
    # --------------------------------------------------------

    optimize!(model)

    if !has_values(model)

        println("No feasible solution found.")

        return nothing
    end

    println(
        "Optimization status: ",
        termination_status(model)
    )


    # --------------------------------------------------------
    # Extract solution
    # --------------------------------------------------------

    assignments = Tuple{String,String}[]
    reassignments = Tuple{String,String}[]

    gate_by_flight = Dict{String,String}()

    for fid in flight_ids

        for gid in candidates[fid]

            if value(x[(fid, gid)]) > 0.5

                push!(assignments, (fid, gid))

                gate_by_flight[fid] = gid

                old_gate = flights[fid]["old_gate"]

                if old_gate !== nothing &&
                   string(old_gate) != gid

                    push!(reassignments, (fid, gid))

                end
            end
        end

        if value(u[fid]) > 0.5
            println("Unassigned: $fid")
        end

    end

    return Dict(
        "assignments" => assignments,
        "reassignments" => reassignments,
        "gate_by_flight" => gate_by_flight,
        "objective" => objective_value(model),
        "status" => string(termination_status(model))
    )

end


# ============================================================
# EVENT STREAM PROCESSING
# ============================================================

function information_time_minutes(value)

    s = replace(strip(string(value)), ":" => "")

    if length(s) == 3
        s = "0" * s
    end

    if length(s) != 4
        error("Unexpected information time: $value")
    end

    return parse(Int, s[1:2]) * 60 +
           parse(Int, s[3:4])

end


function run_event_stream(
    filename,
    gates,
    static_info
)

    println("\nReading scenario: $filename")

    data = JSON.parse(read(filename, String))

    flights = Dict{String, Any}()

    outages = Vector{Dict{String, Any}}()

    old_assignments = Dict{String, String}()

    results = Dict{String, Any}()


    # Process information times chronologically.

    event_times = sort(
        collect(keys(data)),
        by=information_time_minutes
    )

    for information_time in event_times

        println("\n========================================")
        println("Processing information time: $information_time")
        println("========================================")

        events = data[information_time]


        # Apply events.

        for event in events

            apply_event!(
                event,
                flights,
                outages,
                old_assignments
            )

        end

        println("Flights in schedule: ", length(flights))


        # Reoptimize gate assignments.

        result = optimize_gates(
            flights,
            gates,
            outages,
            static_info
        )

        if result === nothing

            println("Could not obtain a feasible assignment.")
            continue

        end


        # Carry assignments forward to the next information time.

        old_assignments = copy(
            result["gate_by_flight"]
        )

        for (fid, flight) in flights

            flight["old_gate"] = get(
                old_assignments,
                fid,
                nothing
            )

        end


        results[string(information_time)] = result


        # Print assignments.

        println("\nAssignments:")

        for (fid, gid) in result["assignments"]
            println("  $fid -> $gid")
        end


        println("\nReassignments:")

        for (fid, gid) in result["reassignments"]
            println("  $fid -> $gid")
        end


        println("\nObjective value: ", result["objective"])

    end

    return results

end


# ============================================================
# SAVE RESULTS
# ============================================================

function save_results(results, filename)

    output = Dict{String, Any}()

    for (information_time, result) in results

        output[information_time] = Dict(

            "assignments" => [
                Dict(
                    "flight" => fid,
                    "gate" => gid
                )
                for (fid, gid) in result["assignments"]
            ],

            "reassignments" => [
                Dict(
                    "flight" => fid,
                    "gate" => gid
                )
                for (fid, gid) in result["reassignments"]
            ],

            "objective" => result["objective"],

            "status" => result["status"]

        )

    end

    open(filename, "w") do file
        JSON.print(file, output, 4)
    end

    println("\nResults saved to $filename")

end


# ============================================================
# MAIN PROGRAM
# ============================================================

function main()

    base_dir = @__DIR__

    # Environment variables allow the Python visualizer
    # to specify the files it wants to run.

    scenario_file = get(
        ENV,
        "GMS_SCENARIO",
        joinpath(base_dir, "busy_day.json")
    )

    static_file = get(
        ENV,
        "GMS_STATIC",
        joinpath(base_dir, "static.json")
    )

    output_file = get(
        ENV,
        "GMS_RESULTS",
        joinpath(base_dir, "results.json")
    )


    # Support either static.json or static_info.json
    # when no environment variable specifies the file.

    if !isfile(static_file) &&
       !haskey(ENV, "GMS_STATIC")

        alternative = joinpath(base_dir, "static_info.json")

        if isfile(alternative)
            static_file = alternative
        end
    end


    # Validate input files.

    if !isfile(scenario_file)
        error("Scenario file not found: $scenario_file")
    end

    if !isfile(static_file)
        error("Static file not found: $static_file")
    end


    # Load static information once.

    println("Loading static information: $static_file")

    static_info = load_static_info(static_file)


    # Load gates from static.json.

    gates = get_gates(static_info)

    println("Loaded $(length(gates)) gates.")

    println(
        "Loaded $(length(static_info["aircraft_info"])) aircraft types."
    )

    println(
        "Loaded $(length(static_info["stations"])) stations."
    )


    # Run the scenario.

    results = run_event_stream(
        scenario_file,
        gates,
        static_info
    )


    # Save results.

    save_results(results, output_file)

end


# ============================================================
# EXECUTE
# ============================================================

main()
