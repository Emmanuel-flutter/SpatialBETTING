# Opta MA18DP Reference -- Event Types and Qualifier Types

Source: Flutter-Global/sockb (suppliers/opta/reference/)

## Message Structure

The topic `trading.{env}.opta.soccer.raw` carries raw MA18DP data: Opta Bookmaker
WebSocket messages wrapped in a trading-header envelope. Format: JSON.

Each message has a `header` (with `msgId`) and the Opta payload containing `events`
and aggregated statistics.

## Critical Parsing Rules

1. **Upsert on event `id`.** The same event can be re-pushed with a new, higher `seqId`.
   Treat the highest `seqId` as authoritative.
2. **typeId 43 = Deleted event.** The event keeps its `id` but its `typeId` changes to `43`.
   Qualifier `144` gives the original typeId.
3. **typeId 84 = Deleted After Review.** Same as deletion but linked to a VAR Contentious
   Referee Decision. Qualifier `436` gives the pre-review typeId.
4. **Qualifier 492 (Goal Confirmed)** is re-pushed onto the Goal event once VAR clears it.
5. **Qualifier 495 (Possible VAR)** is attached to Start Delay to signal a likely VAR check.
6. **typeId 38/39 = Temp Goal / Temp Attempt.** These are provisional; they will change to
   typeId 16 (Goal) or 15 (Attempt Saved) once Opta adds detail qualifiers.

---

## Event Types (typeId)

### Match-critical events

| typeId | Name | Description |
|--------|------|-------------|
| 16 | Goal | Goal scored. outcome always 1. Check qualifier 28 for own goal. |
| 15 | Attempt Saved | Shot saved by keeper or blocked by defender. |
| 14 | Post | Shot hits the frame of the goal. |
| 13 | Miss | Shot projected wide or over the goal. |
| 17 | Card | Yellow (qual 31), second yellow (qual 32), or red (qual 33). |
| 9 | Penalty (via qual) | Qualifier 9 on events 4/10/13/14/15/16 indicates penalty. |
| 38 | Temp Goal | Provisional goal, will become typeId 16. |
| 39 | Temp Attempt | Provisional shot, will become typeId 15. |
| 43 | Deleted event | Event removed. Qualifier 144 holds original typeId. |
| 84 | Deleted After Review | VAR deletion. Qualifier 436 holds pre-review typeId. |

### Play events

| typeId | Name | Description |
|--------|------|-------------|
| 1 | Pass | Pass attempt. outcome 0=unsuccessful, 1=successful. |
| 2 | Offside Pass | Pass where receiver called offside. |
| 3 | Take On | Dribble attempt past opponent. |
| 4 | Foul | Foul committed (outcome 0) or won (outcome 1). Comes in pairs. |
| 5 | Out | Ball out of play for goal kick or throw in. |
| 6 | Corner Awarded | Corner kick awarded. Comes in pairs. |
| 7 | Tackle | Legal ground challenge winning the ball. |
| 8 | Interception | Player intercepts opposition pass. |
| 10 | Save | Goalkeeper save. Qualifier 94 = outfield blocked shot. |
| 11 | Claim | Goalkeeper catches aerial ball. |
| 12 | Clearance | Defensive clearance. |
| 41 | Punch | Goalkeeper punches the ball. |
| 44 | Aerial | Aerial duel. Comes in pairs. outcome 0=lost, 1=won. |
| 45 | Challenge | Failed tackle attempt (always outcome 0). |
| 49 | Ball recovery | Player gathers loose ball. |
| 50 | Dispossessed | Player tackled and loses possession. |
| 51 | Error | Error leading to opposition shot (qual 169) or goal (qual 170). |
| 61 | Ball touch | Bad touch losing possession. |
| 74 | Blocked Pass | Player blocks opposition pass in close proximity. |

### Match control events

| typeId | Name | Description |
|--------|------|-------------|
| 32 | Start | Start of a match period. |
| 30 | End | End of a match period. Qual 308=extra time, 309=penalties. |
| 34 | Team set up | Lineup and formation (quals 30, 130, 131). |
| 40 | Formation change | Team changes formation mid-match. |
| 27 | Start delay | Referee stops game (injury, VAR). Qual 364=VAR delay, 495=possible VAR. |
| 28 | End delay | Stoppage ends, play resumes. |
| 37 | Collection End | Match over, data collection complete. Qual 229=post-match QC done. |
| 18 | Player off | Substituted off. Qual 41=injury, 42=tactical. |
| 19 | Player on | Substitute comes on. |
| 65 | Contentious referee decision | Major talking point or referee error. |
| 70 | Injury Time Announcement | Qual 277 = minutes of added time. |

---

## Qualifier Types (qualifierId)

### Spatial / coordinate qualifiers

| qualifierId | Name | Value | Description |
|-------------|------|-------|-------------|
| 140 | Pass End X | 0.0-100.0 | X coordinate of pass/clearance end point. |
| 141 | Pass End Y | 0.0-100.0 | Y coordinate of pass/clearance end point. |
| 102 | Goalmouth Y | 0-100 | Y coordinate where shot crossed/projected to cross goal line. |
| 103 | Goalmouth Z | 0-100 | Height (Z) where shot crossed goal line. |
| 146 | Blocked X | string | X coordinate where shot was blocked. |
| 147 | Blocked Y | string | Y coordinate where shot was blocked. |
| 230 | GK X Coordinate | string | X position of goalkeeper for the shot. |
| 231 | GK Y Coordinate | string | Y position of goalkeeper for the shot. |
| 127 | Direction of Play | string | "Left to Right" or "Right to Left" relative to TV cameras. |
| 212 | Length | string | Estimated pass/clearance length in metres. |
| 213 | Angle | string | Pass/clearance angle in radians (0.00-6.28). |

### Pitch zone qualifiers (shot location)

| qualifierId | Zone |
|-------------|------|
| 16 | Small box - Centre |
| 17 | Box - Centre |
| 18 | Out of box - Centre |
| 19 | 35+ Centre |
| 60 | Small box - Right |
| 61 | Small box - Left |
| 62 | Box - Deep Right |
| 63 | Box - Right |
| 64 | Box - Left |
| 65 | Box - Deep Left |
| 56 | Zone (value: Back/Left/Center/Right) |

### Shot qualifiers

| qualifierId | Name | Description |
|-------------|------|-------------|
| 15 | Head | Action made with head. |
| 20 | Right footed | Action made with right foot. |
| 72 | Left footed | Action made with left foot. |
| 21 | Other body part | Knee, chest, etc. |
| 82 | Blocked | Shot blocked. |
| 94 | Def block | Shot blocked by outfield defender (on Save event). |
| 100 | Six Yard Blocked | Blocked inside six yard box. |
| 101 | Saved Off Line | Last-line block preventing certain goal. |
| 133 | Deflection | Shot deflected by opponent without changing outcome. |
| 136 | Keeper Touched | Goal scored but keeper got a touch. |
| 138 | Hit Woodwork | Ball hit post or crossbar. |
| 139 | Own Player | Shot saved was deflected by teammate. |
| 153 | Not past goal line | Off-target shot that fails to cross the goal line. |
| 214 | Big chance | Clear-cut chance, player should have scored. |
| 215 | Individual play | Player created the chance themselves. |
| 263 | Direct corner | Shot/goal directly from a corner. |
| 300 | Solo Run | Goal from a solo run. |
| 301 | Shot from cross | Attempt came from a cross. |
| 316 | Passed Penalty | Player passed the ball instead of shooting on penalty. |
| 328 | First Touch | Shot taken first time, no controlling touch. |
| 338 | Follows a Rebound | Reference to previous shot this followed. |
| 389 | Sliding | Sliding action on save or touch. |

### Play situation qualifiers

| qualifierId | Name | Description |
|-------------|------|-------------|
| 22 | Regular play | Event in open play. |
| 23 | Fast break | Event during/following fast break. |
| 24 | Set piece | Following a free kick (not direct). |
| 25 | From corner | Following a corner. |
| 26 | Free kick | Direct free kick shot or first-time shot after tap. |
| 89 | 1 on 1 | One-on-one with goalkeeper. |
| 279 | Kick Off | Starting pass. S=period start, G=after goal. |
| 278 | Tap | Ball tapped to teammate (kick-off or free kick). |

### Pass qualifiers

| qualifierId | Name | Description |
|-------------|------|-------------|
| 1 | Long ball | Pass over 32 metres. |
| 2 | Cross | Ball played from wide areas into the box. |
| 3 | Head pass | Pass made with head. |
| 4 | Through ball | Ball played through for attacking run. |
| 5 | Free kick taken | Any free kick taken. |
| 6 | Corner taken | All corners taken. |
| 152 | Direct | Direct free kick. |
| 154 | Intentional Assist | Pass creating a goal-scoring chance. |
| 168 | Flick-on | Header flick forward to teammate. |
| 195 | Pull back | Pass cut back from by-line in box. |
| 196 | Switch of play | Cross-field pass >60 on Y axis. |
| 210 | Assist | Pass assisted a shot (value = shot typeId). |
| 236 | Blocked pass | Pass blocked by close opponent. |
| 240 | GK Start | Goalkeeper pass after collecting ball. |
| 241 | Indirect | Indirect free kick. |
| 345 | Overhit cross | Cross went over intended target. |

### Card / foul qualifiers

| qualifierId | Name | Description |
|-------------|------|-------------|
| 9 | Penalty | Penalty awarded or taken. |
| 10 | Handball | Foul/card for handball. |
| 12 | Dangerous play | Foul/card for dangerous play. |
| 13 | Foul | General foul. |
| 31 | Yellow Card | Yellow card issued. |
| 32 | Second yellow | Second yellow card. |
| 33 | Red Card | Straight red card. |
| 95 | Back pass | Illegal back pass picked up by keeper. |
| 132 | Simulation | Diving/simulation. |
| 289 | Denied goal-scoring opp | Foul denying a clear chance. |
| 294 | Shove/Push | Foul for pushing. |
| 295 | Shirt Pull/Holding | Foul for holding. |
| 296 | Elbow/Violent Conduct | Foul for violent conduct. |
| 313 | Illegal Restart | Foul for illegal restart. |

### VAR qualifiers

| qualifierId | Name | Description |
|-------------|------|-------------|
| 364 | VAR Delay | Start delay is for a VAR review. |
| 492 | Goal Confirmed | Goal is final, VAR checks concluded. Re-pushed onto Goal event. |
| 495 | Possible VAR | Early warning: delay likely due to VAR. On Start Delay event. |
| 334 | VAR - Other | VAR looking at another situation. |
| 355 | Entering referee review area | Yellow card for entering review area. |
| 356 | Excessive usage of review signal | Yellow card for review signal abuse. |
| 357 | Entering video operations room | Red card for entering VAR room. |
| 365 | Reviewed event ID | Links CRD to the event under review. |
| 436 | Pre-Review Event Type | On typeId 84 (Deleted After Review), holds original typeId. |

### Betting / fantasy qualifiers

| qualifierId | Name | Description |
|-------------|------|-------------|
| 28 | Own Goal | Goal event was an own goal. |
| 280 | Fantasy Assist Type | Type of fantasy assist (PASS_LOST, PENALTY_WON, etc). |
| 281 | Fantasy Assisted By | Player UUID who made the fantasy assist. |
| 282 | Fantasy Assist Team | Team UUID for fantasy assist. |
| 458 | Not assisted | Explicitly marks no assist on the attempt. |

### Linking / identity qualifiers

| qualifierId | Name | Description |
|-------------|------|-------------|
| 7 | Players caught offside | UUID of the player caught offside. |
| 30 | Involved | Array of player/coach IDs in lineup. |
| 44 | Player position | Position in formation (1-5 or name string). |
| 53 | Injured player ID | UUID of the injured player causing delay. |
| 55 | Related event ID | Event ID of the related event (assist for shots). |
| 130 | Team Formation | Formation ID. |
| 131 | Team Player Formation | Player positions in formation (0-11). |
| 144 | Deleted Event Type | Original typeId of a deleted event. |
| 145 | Formation slot | Position assumed by substitute. |
| 189 | Not visible | Event occurred off camera. |
| 194 | Captain | UUID of team captain. |
| 197 | Team kit | Kit ID used by team. |
| 233 | Opposite related event ID | Links paired events (aerials, fouls). |
| 285 | Defensive | Indicates a defensive duel. |
| 286 | Offensive | Indicates an offensive duel. |
| 307 | Pressure | Player under pressure during the action. |
| 312 | Retained ball | Player retained ball after action. |
| 348 | Penalty taker ID | UUID of the penalty taker before kick. |
| 362 | Viral | Something interesting happened (1=Player, 2=Referee, 3=Fans, 4=Manager, 5=Extraordinary). |

### Match control qualifiers

| qualifierId | Name | Description |
|-------------|------|-------------|
| 41 | Injury | Substitution/delay due to injury. |
| 42 | Tactical | Substitution for tactical reasons. |
| 209 | Game end | End event is at the end of the match. |
| 226 | Suspended | Game is suspended. |
| 229 | Post match complete | Post-match QC done. |
| 277 | Minutes | Injury time minutes announced. |
| 299 | Clock Affecting | Delay has affected the match clock. |
| 302 | Checks complete | Live collection checks complete. |
| 308 | Goes to Extra Time | Match will go to extra time. |
| 309 | Goes to Penalties | Match will go to penalties. |
| 325 | Abandonment To Follow | Game is about to be abandoned. |
| 344 | Video coverage lost | Coverage/analysis interrupted. |
| 488 | Time Violation | GK held ball 8+ seconds, corner awarded. |
