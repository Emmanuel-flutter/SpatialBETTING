# SpatialBETTING -- Architecture

## 1. Purpose

SpatialBETTING is a PoC that consumes live Opta football event data from Confluent Cloud Kafka and explores spatial betting use cases built on top of that data.

## 2. System overview

```
Opta WebSocket (MA18DP)
       |
   [SDSCO]  (Opta Bookmaker Connector -- not ours)
       |
   Kafka: trading.{env}.opta.soccer.raw
       |
   [SpatialBETTING consumer]  <-- this project
       |
   local processing / analysis
```

The project reads from an existing Kafka topic produced by SDSCO. It does not produce to any topic.

## 3. Infrastructure

| Resource | Value |
|---|---|
| Service Account | `test-spatialbetting-disruptive-innovation-sa` |
| SA ID | `sa-z33pmjy` |
| Environment | `test-uki` (`env-mvr792`) |
| Cluster | shared-cluster (`lkc-zg6w6d`) |
| Bootstrap server | see `.env` |
| Topics (read) | `trading.qa.opta.soccer.raw`, `trading.nxt.opta.soccer.raw` |
| Consumer group | `spatialbetting-disruptive-innovation` |
| Vault path | `team_confluent_cloud/ci/kafka-keys/test-spatialbetting-disruptive-innovation-sa/<KEY_ID>` |

### 3.1 IAM provenance

| Step | Repo | PR |
|---|---|---|
| Service Account | `Flutter-Global/cc-global-iam` | #1801 |
| RBAC (DataDiscovery) | `Flutter-Global/cc-global-iam` | #1825 |
| API Key (kafka) | `Flutter-Global/cc-global-iam` | #1816 |
| Vault access | `gitlab.app.betfair/security/vault-access-control` | MR #7683 |
| Topic ACLs (READ) | `Flutter-Global/ccloud-terraform-uki-shared` | #959 |

## 4. Data model

Each Kafka message is JSON with two top-level keys:

### 4.1 `header` (trading envelope)

| Field | Type | Description |
|---|---|---|
| `msgId` | UUID | Unique message identifier |
| `bodyAction` | string | Always `CREATE` |
| `bodyType` | string | Always `STATE` |
| `correlationTimestamp` | ISO 8601 | Opta source timestamp |
| `timestamp` | ISO 8601 | Connector publish timestamp |
| `systemId` | string | Always `opta-bookmaker-connector` |
| `entityId` | string | Opta fixture ID |
| `sequenceId` | int | Monotonic sequence for ordering |

### 4.2 `body.content.liveData.matchDetails`

Contains the Opta MA18DP payload:

- `event[]` -- array of match events, each with `id`, `seqId`, `typeId`, `periodId`, `timeMin`, `timeSec`, `contestantId`, `playerId`, `playerName`, `outcome`, `x`, `y`, `qualifier[]`.
- `stats.team[]` -- running aggregated team/player statistics.

### 4.3 `body.fixture`

Match metadata: `fixtureId`, `competitionId`, `tournamentId`, `startTime`, `teams[]` (id, name, association HOME/AWAY).

## 5. Critical parsing rules

1. **Upsert on event `id`.** The same event can be re-pushed with a higher `seqId`. Treat the highest `seqId` as authoritative.
2. **typeId 43 = deleted event.** The event keeps its `id` but its `typeId` changes to 43.
3. **Qualifier 492 = Goal Confirmed.** Arrives as a re-push of the original Goal event (typeId 16) after VAR review.
4. **Qualifier 495 = Possible VAR.** Arrives against Start Delay event (typeId 27).
5. **Coordinates** -- `x`, `y` are percentage pitch coordinates (0-100). Qualifiers 140/141 give end coordinates, 102/103 give goalmouth coordinates (212/213 are pass length and angle).

## 6. Project structure

```
SpatialBETTING/
  config/          -- centralised configuration, reads from .env
  services/        -- business logic modules
    consumer.py    -- Kafka consumer with callback interface
  docs/            -- reference material (Opta event/qualifier types)
  consumer.py      -- entry point: run the consumer
  requirements.txt -- pip dependencies
  .env             -- credentials (gitignored)
```

## 7. Running locally

```bash
source venv/bin/activate
python consumer.py
```

The consumer subscribes to `trading.nxt.opta.soccer.raw` by default. Override with `CONFLUENT_TOPIC` in `.env`.

## 8. Contract constraints

Opta data is heavily licensed under Schedule 1 Parts A, I, J, K, N and P. Key restrictions:

- Betting markets may only use a closed list of Permitted Data Points.
- In-play markets are permitted on Tier 1 matches only (Part A).
- AI/model-training use is prohibited under Clause 11.2.6 -- interpretation routed to Legal.
- Attribution is mandatory on any consumer-facing display.

See `docs/opta_reference.md` and `Flutter-Global/sockb/suppliers/opta/contract/` for the full contract entitlements.
