# Architecture

## Current state

Phase 0.1 provides an executable Python package and a minimal Next.js web
application. They do not exchange data and no external service is connected.

## Planned direction

Python will own collection, data processing, NLP, and ML concerns. Next.js and
TypeScript will provide the web interface. Concrete boundaries and integration
contracts will be introduced only with the relevant vertical slices.
