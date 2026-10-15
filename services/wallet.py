class Wallets:
    """Play-money balances by player id, shared by every game and fixture in this process.

    Held in memory only, so a server restart resets every balance. Only touch it from the event loop.
    """

    def __init__(self, start_balance: float = 1000.0):
        self.start_balance = start_balance
        self._balances: dict[str, float] = {}

    def balance(self, player_id: str) -> float:
        return self._balances.setdefault(player_id, self.start_balance)

    def set(self, player_id: str, value: float) -> None:
        self._balances[player_id] = value
