from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from admin_panel.bd_models.models import BallInstance


@dataclass
class Participant:
	instance: BallInstance
	max_hp: int
	current_hp: int
	shield_percent: float = 0.0
	extra_damage_multiplier: float = 1.0
	extra_flat_damage: int = 0


class AbilityProcessor:
	@staticmethod
	def apply_abilities(when: str, actor: Participant, target: Optional[Participant]) -> List[str]:
		"""
		Apply abilities defined in the actor's ball.capacity_logic.
		Expected structure: {"on_enter":[], "on_attack": [], "on_defend": [], "on_exit": []}
		Supported ability types: damage_multiplier, extra_damage, heal, shield
		"""
		logs: List[str] = []
		logic: Dict[str, Any] = actor.instance.countryball.capacity_logic or {}
		for entry in logic.get(when, []):
			t = entry.get("type")
			if t == "damage_multiplier":
				mult = float(entry.get("value", 1.0))
				logs.append(f"{actor.instance.short_description()} uses damage x{mult}.")
				actor.extra_damage_multiplier *= mult
			elif t == "extra_damage":
				amt = int(entry.get("value", 0))
				logs.append(f"{actor.instance.short_description()} gains +{amt} extra damage this attack.")
				actor.extra_flat_damage += amt
			elif t == "heal":
				pct = float(entry.get("value", 0.0))
				heal = max(1, int(actor.max_hp * pct))
				actor.current_hp = min(actor.current_hp + heal, actor.max_hp)
				logs.append(f"{actor.instance.short_description()} heals {heal} HP.")
			elif t == "shield":
				pct = float(entry.get("value", 0.0))
				if target:
					target.shield_percent = max(target.shield_percent, pct)
					logs.append(
						f"{actor.instance.short_description()} grants a {int(pct*100)}% shield to {target.instance.short_description()}."
					)
			else:
				logs.append(f"Unknown ability type: {t}")

		return logs


class TeamBattle:
	"""Simulate a battle between two teams (lists) of up to 3 BallInstances each."""

	def __init__(self, team_a: List[BallInstance], team_b: List[BallInstance]):
		if not team_a or not team_b:
			raise ValueError("Both teams must have at least one BallInstance")

		def make_participants(team: List[BallInstance]) -> List[Participant]:
			parts: List[Participant] = []
			for inst in team[:3]:
				maxhp = inst.health
				parts.append(Participant(instance=inst, max_hp=maxhp, current_hp=maxhp))
			return parts

		self.team_a = make_participants(team_a)
		self.team_b = make_participants(team_b)

	def _active(self, team: List[Participant]) -> Optional[Participant]:
		for p in team:
			if p.current_hp > 0:
				return p
		return None

	def run(self) -> List[str]:
		logs: List[str] = []
		turn = 1

		# trigger on_enter for initial participants
		a_active = self._active(self.team_a)
		b_active = self._active(self.team_b)
		if a_active:
			logs += AbilityProcessor.apply_abilities("on_enter", a_active, b_active)
		if b_active:
			logs += AbilityProcessor.apply_abilities("on_enter", b_active, a_active)

		while True:
			a_active = self._active(self.team_a)
			b_active = self._active(self.team_b)
			if not a_active or not b_active:
				break

			logs.append(f"-- Turn {turn}: {a_active.instance.short_description()} vs {b_active.instance.short_description()} --")

			# team A attacks
			logs += AbilityProcessor.apply_abilities("on_attack", a_active, b_active)
			dmg = int(a_active.instance.attack * a_active.extra_damage_multiplier) + a_active.extra_flat_damage
			logs += AbilityProcessor.apply_abilities("on_defend", b_active, a_active)

			if b_active.shield_percent:
				reduction = int(dmg * b_active.shield_percent)
				dmg -= reduction
				logs.append(f"{b_active.instance.short_description()} absorbs {reduction} damage with shield.")

			dmg = max(1, dmg)
			b_active.current_hp -= dmg
			logs.append(f"{a_active.instance.short_description()} deals {dmg} damage. {b_active.instance.short_description()} HP is now {max(b_active.current_hp,0)}.")

			a_active.extra_damage_multiplier = 1.0
			a_active.extra_flat_damage = 0

			if b_active.current_hp <= 0:
				logs.append(f"{b_active.instance.short_description()} has been defeated.")
				logs += AbilityProcessor.apply_abilities("on_exit", b_active, a_active)
				next_def = self._active(self.team_b)
				if next_def:
					logs += AbilityProcessor.apply_abilities("on_enter", next_def, a_active)

			# team B attacks (if still alive)
			a_active = self._active(self.team_a)
			b_active = self._active(self.team_b)
			if not a_active or not b_active:
				break

			logs += AbilityProcessor.apply_abilities("on_attack", b_active, a_active)
			dmg = int(b_active.instance.attack * b_active.extra_damage_multiplier) + b_active.extra_flat_damage
			logs += AbilityProcessor.apply_abilities("on_defend", a_active, b_active)

			if a_active.shield_percent:
				reduction = int(dmg * a_active.shield_percent)
				dmg -= reduction
				logs.append(f"{a_active.instance.short_description()} absorbs {reduction} damage with shield.")

			dmg = max(1, dmg)
			a_active.current_hp -= dmg
			logs.append(f"{b_active.instance.short_description()} deals {dmg} damage. {a_active.instance.short_description()} HP is now {max(a_active.current_hp,0)}.")

			b_active.extra_damage_multiplier = 1.0
			b_active.extra_flat_damage = 0

			if a_active.current_hp <= 0:
				logs.append(f"{a_active.instance.short_description()} has been defeated.")
				logs += AbilityProcessor.apply_abilities("on_exit", a_active, b_active)
				next_a = self._active(self.team_a)
				if next_a:
					logs += AbilityProcessor.apply_abilities("on_enter", next_a, b_active)

			turn += 1
			if turn > 1000:
				logs.append("Turn limit reached, ending in a draw.")
				break

		a_alive = any(p.current_hp > 0 for p in self.team_a)
		b_alive = any(p.current_hp > 0 for p in self.team_b)
		if a_alive and not b_alive:
			logs.append("Team A wins!")
		elif b_alive and not a_alive:
			logs.append("Team B wins!")
		else:
			logs.append("Battle ended in a draw.")

		return logs


