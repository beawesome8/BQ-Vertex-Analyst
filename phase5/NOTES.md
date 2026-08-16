# Phase 5 Notes

FastAPI service wrapping the full agent pipeline (`phase2.agent_core`'s
compiled LangGraph), plus a thin Streamlit demo UI that talks to it over
HTTP.

## Design decision, verified before writing the endpoints

Both `/answer` and `/suggest` are plain `def`, not `async def`. Reason:
every client used across this project (Gemini via `google-genai`,
BigQuery) is synchronous. An `async def` endpoint calling a blocking
client stalls FastAPI's single event loop for the full duration of the
call, serializing all concurrent requests regardless of how many users
hit the API.

Measured this directly rather than citing it from memory or
documentation: built a throwaway test app with a sync endpoint and an
async endpoint, both calling a 0.5-second blocking `time.sleep()`, fired
5 concurrent requests at each via `httpx.AsyncClient`. Sync endpoint: 0.54s
total (genuinely parallel, run in Starlette's thread pool). Async
endpoint: 2.51s total (fully serialized -- 5 x 0.5s back to back). The
design choice is based on a measured number, not an assumption about how
FastAPI behaves.

## Verification approach

Could not test against live Gemini/BigQuery from the build sandbox (same
constraint as every phase since Phase 2) -- but could and did test the
service layer itself by monkeypatching the compiled graph's `.invoke()`
method, which meant testing against the REAL `service.py` importing the
REAL `phase2/phase3/phase4` packages, not stubs:

1. `/health` returns 200 without invoking the agent at all.
2. Empty question string -> 422.
3. Missing `question` field entirely -> 422 (Pydantic validation, not
   custom code).
4. A canned "passed" result serializes correctly through the full
   response model.
5. A canned "blocked" result correctly shows `null` for every execution
   field -- confirming the API layer faithfully surfaces the structural
   guarantee built in Phase 4's conditional routing (a blocked query
   never executes) all the way out to the HTTP response, not just
   internal state.
6. The agent pipeline raising an exception returns a clean 500 with a
   message, not an unhandled crash.
7. `/suggest` works on the same pattern.

Not tested: the actual Streamlit UI (`app.py`) and the full pipeline
through a live server hitting real Gemini/BigQuery. That's the first
thing to verify live, same as every phase before this one -- syntax-clean
is not the same claim as working.

## Explicit scope boundaries (not oversights)

- CORS is wide open (`allow_origins=["*"]`) -- local demo only, flagged
  in the file itself so it's never mistaken for production-appropriate.
- No auth, no rate limiting, no streaming responses. This is a portfolio
  demo service, not a hardened API.
- The graph is compiled once at import time and reused across requests
  (not rebuilt per-request) -- the graph structure never changes between
  calls, so rebuilding it would be pure waste.

## Frontend choice

Phase plan left "Streamlit or React/Vite" open. Chose Streamlit:
faster to build and verify, and the FastAPI service is the part that
actually demonstrates engineering depth -- the frontend is a thin demo
shell either way, not worth the extra build time of a React/Vite setup
for a single-user local demo.
