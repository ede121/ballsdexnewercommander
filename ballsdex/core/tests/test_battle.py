from ballsdex.core.battle import TeamBattle, AbilityProcessor


class _MockBall:
    def __init__(self, health=10, attack=3, capacity_logic=None, country="Mock"):
        self.health = health
        self.attack = attack
        self.capacity_logic = capacity_logic or {}
        self.country = country


class _MockInstance:
    def __init__(self, ball: _MockBall, pk=1):
        self._ball = ball
        self.pk = pk

    @property
    def health(self):
        return self._ball.health

    @property
    def attack(self):
        return self._ball.attack

    @property
    def countryball(self):
        return self._ball

    def short_description(self, *args, **kwargs):
        return f"#{self.pk} {self._ball.country}"


def test_simple_1v1_damage():
    a = _MockInstance(_MockBall(health=20, attack=5))
    b = _MockInstance(_MockBall(health=10, attack=1))
    tb = TeamBattle([a], [b])
    logs = tb.run()
    assert any("wins" in line.lower() for line in logs)


def test_ability_extra_damage_and_shield():
    a_ball = _MockBall(health=20, attack=5, capacity_logic={"on_attack": [{"type": "extra_damage", "value": 3}]})
    b_ball = _MockBall(health=30, attack=4, capacity_logic={"on_defend": [{"type": "shield", "value": 0.5}]})
    a = _MockInstance(a_ball, pk=10)
    b = _MockInstance(b_ball, pk=20)
    tb = TeamBattle([a], [b])
    logs = tb.run()
    # ensure logs show extra damage and shield messages
    assert any("extra damage" in line.lower() for line in logs)
    assert any("shield" in line.lower() or "absorbs" in line.lower() for line in logs)


def test_heal_and_multiplier():
    a_ball = _MockBall(health=10, attack=4, capacity_logic={"on_attack": [{"type": "damage_multiplier", "value": 2.0}], "on_enter": [{"type": "heal", "value": 0.5}]})
    b_ball = _MockBall(health=30, attack=2)
    a = _MockInstance(a_ball, pk=50)
    b = _MockInstance(b_ball, pk=60)
    tb = TeamBattle([a], [b])
    logs = tb.run()
    assert any("heals" in line.lower() for line in logs)
    assert any("uses damage x" in line.lower() for line in logs)
*** End Patch