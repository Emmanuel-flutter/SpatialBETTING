"""Opta MA18DP identifiers. Names and meanings come from docs/opta_reference.md."""

EVENT_NAMES = {
    1: "Pass", 2: "Offside pass", 3: "Take on", 4: "Foul", 5: "Out", 6: "Corner awarded",
    7: "Tackle", 8: "Interception", 10: "Save", 11: "Claim", 12: "Clearance", 13: "Miss",
    14: "Post", 15: "Attempt saved", 16: "Goal", 17: "Card", 18: "Player off", 19: "Player on",
    27: "Start delay", 28: "End delay", 30: "End", 32: "Start", 34: "Team set up",
    37: "Collection end", 38: "Temp goal", 39: "Temp attempt", 40: "Formation change",
    41: "Punch", 43: "Deleted event", 44: "Aerial", 45: "Challenge", 49: "Ball recovery",
    50: "Dispossessed", 51: "Error", 55: "Offside provoked", 61: "Ball touch", 65: "Contentious referee decision",
    70: "Injury time announcement", 74: "Blocked pass", 84: "Deleted after review",
}

PASS = 1
OFFSIDE_PASS = 2
FOUL = 4
OUT = 5
CORNER = 6
OFFSIDE_PROVOKED = 55
MISS, POST, ATTEMPT_SAVED, GOAL = 13, 14, 15, 16
SHOTS = frozenset({MISS, POST, ATTEMPT_SAVED, GOAL})
CARD = 17
PLAYER_OFF, PLAYER_ON = 18, 19
START_DELAY = 27
PERIOD_END, PERIOD_START = 30, 32
TEAM_SETUP = 34
TEMP_GOAL = 38
DELETED, DELETED_AFTER_REVIEW = 43, 84
DELETED_TYPES = frozenset({DELETED, DELETED_AFTER_REVIEW})

Q_PENALTY = 9
Q_OWN_GOAL = 28
Q_INVOLVED = 30
Q_YELLOW, Q_SECOND_YELLOW, Q_RED = 31, 32, 33
Q_FORMATION_SLOTS = 131
Q_END_X, Q_END_Y = 140, 141
Q_GOAL_MOUTH_Y, Q_GOAL_MOUTH_Z = 102, 103
Q_GAME_END = 209
Q_GOAL_CONFIRMED = 492
Q_POSSIBLE_VAR = 495
Q_VAR_DELAY = 364
