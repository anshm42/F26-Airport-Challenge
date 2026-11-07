# Overview

Building a prototype that could be built out into a product to sell to Toronto Pearson Airport.

# Architecture

## Feature list
### Static information
In-memory JSON data structure for storing and recalling airplane and gate information.
There are ballpark 1200 flight actions per day, so everything fits in memory; past matching information is the data lake's responsibility.
Above all else, it should be fast, with efficient fetching.
Not fully static: we should be able to insert or modify gates and airplane types on the fly.
Everything that relates to Pearson should be stored here.
Prototype ignores security and compliance.

### Dynamic updates
Standalone server, separate from the matching algorithm.
Pushes updates to the matching algorithm via webhook, so the algorithm doesn't have to poll and can respond faster.
The updater owns the webhook API: message format, headers, delivery order, and acknowledgment behavior. The matching algorithm implements a receiver conforming to that contract. The receiver URL is deployment configuration.
Uses a subset of the ASM spec (https://xwiki.avinor.no/spaces/ADDMIFTM/pages/45711511/ASM+-+Adhoc+Scheduled+Message), with our own extensions.
Reads from a locally hosted data pipe, which doesn't respond to changes and is out of our control.
Should be able to run either deterministic scripts (for smaller cases) or stochastic simulations of high traffic.
Prototype assumes trustworthy data, timely updates, and that any intervention would have an existing system.

### Matching algorithm
Handles initialization and updates efficiently. May be re-implemented in C++ depending on benchmark results.
Optimality is not required; quality and correctness are. We need a good, permissible solution quickly, and it needs to scale.
We need to save audit logs for every decision that's made, so we can come back and compare them with what actually happened, for traceability and locating fault.
Data lake mentality: save everything we can into temporary files.

### Test harness
Runs through update scenarios to measure the performance of the matching algorithm in various cases.
Should be able to compare against more naive algorithms to establish improvement.
Runs a stress test to show that it handles high load well.

### Operator view
Gate-focused view showing what's happening from the top down.
Alarms flag current and potential issues, such as missed schedules, late planes, and tight connections.
Errors and exception cases are handled by prompting the operator for a decision.

### Passenger gate view
Flight-focused view for a passenger inside the airport.
Shows the flight time, gate match, and boarding information: anything needed to find their gate and arrive on time.
Shows delays and reassignments.

## System connections
The matching algorithm is the "meat and potatoes". It reads the static information (which it owns) and receives dynamic updates via webhook from the dynamic updates server. It writes all decisions to an audit log and makes the latest information available via API.
The test harness takes control of the dynamic updates and reads the matching algorithm's output for benchmarking.
The operator and passenger gate views read from the matching algorithm's API and receive real-time updates over WebSocket.

## Known unknowns
- How the test harness ties into the dynamic updates server (replace the data pipe, script the server, or run its own instance?).
- API spec between the matching algorithm and the views (pending).


# Improvements

- Algorithm improvements
	- Includes benchmarking to show empirically
	- Stress testing, scaling, argue that it'll handle load
- More data stored
	- Gate information
	- Plane information
	- Airline subsidiaries
- More data communicated (extend the ASM spec)
	- Fin # / Registration #
	- Codeshare
- Timeline visibility
	- Colour coding
	- Gate changes
	- Codeshares
	- Alarm for tight connections
- Error handling
	- Unknown plane type (Russian refugee, emergency landing, other error case)
	- Outgoing plane type changes (requires changing gate, passengers can move but shouldn't have to move far?)

# New feature

- Passenger gate view
	- We're already assigning gates, why not also display them to passengers?
	- This is the "do everything and just win, lol" angle
