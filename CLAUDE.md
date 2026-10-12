# SpatialBETTING workspace -- working instructions

## ARCHITECTURE.md is the architectural source of truth

Read `ARCHITECTURE.md` before any structural change. It documents the infrastructure (SA, cluster, topics, Vault path, IAM PRs), the data model, parsing rules, and contract constraints.

## Data source

This project consumes from `trading.{env}.opta.soccer.raw` on the UKI Shared Confluent Cloud cluster (test environment). The data is live Opta MA18DP football events produced by SDSCO.

## Credentials

All credentials live in `.env` (gitignored). Never commit secrets. The API key and secret are stored in Vault at `team_confluent_cloud/ci/kafka-keys/test-spatialbetting-disruptive-innovation-sa/`.

## Running

```bash
source venv/bin/activate
python consumer.py
```

## Code structure

- `config/` -- centralised config, all env vars read here
- `services/` -- business logic modules
- `docs/` -- reference material (Opta event types, qualifier types)
- Entry points live at the project root

## Opta parsing rules

1. Upsert on event `id` -- highest `seqId` wins
2. typeId 43 = deleted event
3. Qualifier 492 = Goal Confirmed (re-push after VAR)
4. Qualifier 495 = Possible VAR
5. Coordinates are 0-100 percentage pitch values

## Contract

Opta data is licensed. Clause 11.2.6 prohibits AI/model training. Check `docs/opta_reference.md` and `sockb/suppliers/opta/contract/` before building anything beyond raw consumption.
