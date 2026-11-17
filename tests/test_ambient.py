import datetime as dt
import importlib.machinery
import importlib.util
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ambient import (LightSources, LEVELS, clear_day_level, light_level,
                     solar_elevation, stepped, targets, load, save, matched, add_match)
from gamma_control import curve, lifted
import gamma_control
from monitor_hardware import read_outputs, parse_detect


class Policy(unittest.TestCase):
    def test_level_is_continuous_in_every_light_source(self):
        # Sun only: anchors at −12°, −6°, horizon and 6°, gliding in between, never a jump.
        self.assertEqual([clear_day_level(e) for e in (-30, -12, -6, -.833, 6, 40)], [5, 5, 10, 15, 20, 20])
        self.assertAlmostEqual(clear_day_level(-9), 7.5)
        steps = [clear_day_level(e / 10) for e in range(-200, 200)]
        self.assertLess(max(b - a for a, b in zip(steps, steps[1:])), .12)
        # Indoor lux on a log scale.
        self.assertEqual([round(light_level({'lux': x})) for x in (0, 2.5, 12, 55, 200, 5000)],
                         [5, 5, 10, 15, 20, 20])
        self.assertTrue(10 < light_level({'lux': 25}) < 15)
        # Outdoor radiation: an overcast noon sits at evening level, not night.
        self.assertEqual(light_level({'radiation': 0, 'elevation': 20}), 10)
        self.assertEqual(light_level({'radiation': 150, 'elevation': 20}), 20)
        self.assertAlmostEqual(light_level({'radiation': 75, 'elevation': 20}), 17.5)
        self.assertEqual(light_level({'radiation': 0, 'elevation': -20}), 5)
        # No step where the sun crosses the horizon.
        self.assertAlmostEqual(light_level({'radiation': 0, 'elevation': -.84}),
                               light_level({'radiation': 0, 'elevation': -.82}), places=1)
        # The owner's own anchors are used.
        mine = {'Night': 2, 'Evening': 8, 'Dusk': 20, 'Day': 40}
        self.assertEqual(clear_day_level(30, mine), 40)
        self.assertEqual(light_level({'lux': 0}, mine), 2)

    def test_missing_and_stale_sources(self):
        source = LightSources()
        now = 1794394800
        source.sensor = {'lux': 10, 'at': now, 'age': 5}
        source.weather = {'samples': [(now - 900, 100), (now, 200), (now + 900, 300)], 'at': now}
        self.assertEqual(source.choose({}, now=now)['source'], 'Light sensor')
        source.sensor = None
        estimate = source.choose({}, now=now + 450)
        self.assertEqual(estimate['source'], 'Daylight estimate')
        self.assertEqual(estimate['radiation'], 250)       # glides between quarter-hour samples
        self.assertEqual(source.choose({}, now=now + 900 + 2800)['source'], 'Offline sun position')

    def test_offline_solar_matches_amsterdam_day_night(self):
        noon = dt.datetime(2026, 11, 11, 11, tzinfo=dt.timezone.utc).timestamp()
        midnight = dt.datetime(2026, 11, 11, 23, tzinfo=dt.timezone.utc).timestamp()
        self.assertTrue(15 < solar_elevation(noon) < 25)
        self.assertLess(solar_elevation(midnight), -40)

    def test_main_only_modes_and_auto_following_game(self):
        keys = ['main', 'side']
        result = targets(keys, 'main', 10, {'main': {'mode': 'movie'}})
        self.assertEqual((result['main']['10'], result['main']['12']), (50, 50))
        self.assertEqual((result['side']['10'], result['side']['12']), (10, 20))
        result = targets(keys, 'main', 5, {'main': {'mode': 'game'}})
        self.assertEqual(result['main']['10'], 5)
        self.assertEqual(result['side']['10'], 5)
        result = targets(keys, 'main', 20, {'side': {'mode': 'movie'}})
        self.assertEqual(result['side']['10'], 20)

    def test_manual_hold_and_smooth_ramp(self):
        result = targets(['main'], 'main', 5, {'main': {'mode': 'manual', 'values': {'10': 13}}})
        self.assertEqual(result['main']['10'], 13)
        self.assertEqual(stepped(20, 5), 18)
        self.assertEqual(stepped(6, 5), 5)
        self.assertEqual(stepped(5, 20), 7)

    def test_shadow_lift_moves_only_the_dark_end(self):
        base = [[round(i / 255 * peak) for i in range(256)] for peak in (65535, 40000, 20000)]
        for shadows in (5, 40, 100):
            for old, new in zip(base, lifted(base, shadows)):
                self.assertEqual(new[0], old[0])
                self.assertEqual(new[-1], old[-1])
                self.assertGreater(new[16], old[16])
                self.assertTrue(all(a < b for a, b in zip(new, new[1:])), 'strictly increasing')
                # Upper midtones and highlights are practically untouched.
                self.assertTrue(all(b - a <= old[-1] * .012 for a, b in zip(old[176:], new[176:])))
        self.assertEqual(lifted(base, 0), base)
        # Near-black is lifted clearly, yet with a finite slope (no blown-up noise).
        self.assertGreater(curve(16 / 255, 40), 2 * 16 / 255)
        self.assertLessEqual(curve(1 / 255, 100), 5 / 255)

    def test_redshift_following_updates_base_without_touching_main(self):
        lift = gamma_control.ShadowLift()
        warm = [[0, 65535], [0, 50000], [0, 30000]]
        lift.record = {'base': [[0, 65535]] * 3, 'processes': [{'pid': 1, 'start': 's'}],
                       'output': 1, 'crtc': 10, 'temperature': 6500}

        class FakeRR:
            display = None
            class r:
                XRRGetCrtcGammaSize = staticmethod(lambda d, c: 2)
            crtcs = staticmethod(lambda: [10, 11, 12])
            read = staticmethod(lambda crtc: warm)
        lift.rr = FakeRR()
        with patch.object(gamma_control, 'redshift_temperature', return_value=4000), patch.object(
                gamma_control.subprocess, 'run') as run, patch.object(gamma_control, 'save'):
            run.return_value.returncode = 0
            self.assertEqual(lift.follow_redshift([11, 12]), 4000)
        self.assertEqual([c.args[0][-1] for c in run.call_args_list], ['randr:crtc=1', 'randr:crtc=2'])
        self.assertEqual(lift.record['base'], warm)
        # Switched off in the tray (untinted ramp while Redshift wants warmth): leave it alone.
        lift.record.update({'base': [[0, 65535]] * 3, 'temperature': None})
        with patch.object(gamma_control, 'redshift_temperature', return_value=3000), patch.object(
                gamma_control.subprocess, 'run') as run:
            lift.follow_redshift([11])
            self.assertIsNone(lift.follow_redshift([11]))
            run.assert_not_called()

    def test_matching_monitors_by_eye(self):
        self.assertEqual(matched(10, None), 10)
        # One pair: this monitor at 0 looks like main at 10 → shifted, never below 0.
        one = add_match(None, 10, 0)
        self.assertEqual([matched(l, one) for l in (5, 10, 20, 50)], [0, 0, 10, 40])
        # A second pair bends the line; between and beyond the pairs it stays straight.
        two = add_match(one, 40, 20)
        self.assertEqual([matched(l, two) for l in (10, 25, 40, 70)], [0, 10, 20, 40])
        # A new judgement at about the same level replaces the old one.
        self.assertEqual(add_match(two, 11, 3), [[11, 3], [40, 20]])
        # A pair that contradicts the new one (brighter on main, darker here) is dropped.
        self.assertEqual(add_match(two, 20, 30), [[10, 0], [20, 30]])
        # Two close pairs cannot make the line run away.
        self.assertLessEqual(matched(100, [[10, 0], [15, 40]]), 100)
        result = targets(['main', 'side'], 'main', 20, {}, {'side': one, 'main': one})
        self.assertEqual((result['main']['10'], result['side']['10']), (20, 10))

    def test_atomic_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            save(path, {'lux': 10})
            self.assertEqual(load(path), {'lux': 10})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_sensor_and_weather_failure_fall_back_without_raising(self):
        source = LightSources()
        with patch('ambient.command', side_effect=RuntimeError('offline')), patch(
                'ambient.urllib.request.urlopen', side_effect=OSError('offline')):
            source.poll({'lux_ssh': 'user@sensor'}, now=100000)
        self.assertEqual(source.choose({}, now=100000)['source'], 'Offline sun position')
        self.assertEqual(source.sensor_error, 'offline')

    def test_recovery_resumes_redshift_even_if_display_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / 'recovery.json'
            save(record, {'base': [[0, 65535]] * 3, 'output': 1, 'crtc': 2,
                          'processes': [{'pid': 123, 'start': '456'}]})
            with patch.object(gamma_control, 'RECOVERY', record), patch.object(
                    gamma_control, 'RandR', side_effect=RuntimeError('no display')), patch.object(
                    gamma_control, 'signal_owned') as resume:
                with self.assertRaisesRegex(RuntimeError, 'no display'):
                    gamma_control.recover()
                resume.assert_called_once()
                self.assertTrue(record.exists(), 'Keep recovery evidence for the next attempt')

    def test_recovery_never_signals_a_reused_pid(self):
        with patch.object(gamma_control, 'process_info', return_value={
                'pid': 123, 'start': 'new'}), patch.object(gamma_control.os, 'kill') as kill:
            gamma_control.signal_owned({'pid': 123, 'start': 'old'}, 18)
            kill.assert_not_called()


class Service(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = str(Path(__file__).resolve().parents[1] / 'monitor-automation')
        loader = importlib.machinery.SourceFileLoader('automation_test', path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.service = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.service)

    def engine(self, settings):
        with patch.object(self.service, 'load', side_effect=lambda path, *a: (
                {'ambient': {'main_monitor': 'main'}} if path == self.service.CONFIG else settings)):
            engine = self.service.Engine()
        engine.devices = {}
        return engine

    def handle(self, engine, payload):
        with patch.object(self.service, 'load', return_value={'ambient': {'main_monitor': 'main'}}), \
                patch.object(self.service, 'save'):
            return engine.handle(payload)

    def test_modes_belong_to_main_and_pins_survive_them(self):
        engine = self.engine({'monitors': {}})
        engine.devices = {'main': {}, 'side': {}}
        engine.shadow_lift = lambda *a, **k: ([], None)
        self.handle(engine, {'op': 'set', 'key': 'side', 'values': {'10': 7}})
        self.handle(engine, {'op': 'mode', 'mode': 'game'})
        self.handle(engine, {'op': 'set', 'key': 'main', 'values': {'10': 30}})
        self.assertEqual(engine.settings['monitors']['main'], {'mode': 'game', 'values': {'10': 30}})
        # Unpinning main inside Game drops its tuning but keeps Game.
        self.handle(engine, {'op': 'release', 'key': 'main'})
        self.assertEqual(engine.settings['monitors']['main'], {'mode': 'game'})
        # Leaving Game does not wipe the side monitor's pinned level.
        self.handle(engine, {'op': 'mode', 'mode': 'auto'})
        self.assertEqual(engine.settings['monitors'], {'side': {'mode': 'manual', 'values': {'10': 7}}})
        self.handle(engine, {'op': 'release', 'key': 'all'})
        self.assertEqual(engine.settings['monitors'], {})
        self.assertIn('side', engine.snap_keys)

    def test_match_request_records_pairs_and_replaces_pins(self):
        engine = self.engine({'monitors': {'side': {'mode': 'manual', 'values': {'10': 0}}}})
        engine.devices = {'main': {}, 'side': {}}
        self.handle(engine, {'op': 'match', 'level': 10, 'points': {'main': 10, 'side': 0, 'gone': 5}})
        self.assertEqual(engine.settings['match'], {'side': [[10, 0]]})
        self.assertEqual(engine.settings['monitors'], {})
        self.handle(engine, {'op': 'match', 'forget': 'side'})
        self.assertEqual(engine.settings['match'], {})
        with self.assertRaises(ValueError):
            self.handle(engine, {'op': 'match', 'level': 10, 'points': {'side': 300}})

    def test_day_curve_anchors_and_immediate_slider_writes(self):
        engine = self.engine({'monitors': {}})
        engine.devices = {'main': {'bus': 5}, 'side': {'bus': 4}}
        self.assertEqual(engine.settings['levels'], LEVELS)
        self.handle(engine, {'op': 'levels', 'values': {'Night': 3, 'Evening': 12, 'Dusk': 10, 'Day': 40}})
        # Kept non-decreasing: brighter light never means a darker screen.
        self.assertEqual(engine.settings['levels'], {'Night': 3, 'Evening': 12, 'Dusk': 12, 'Day': 40})
        with self.assertRaises(ValueError):
            self.handle(engine, {'op': 'levels', 'values': {'Night': 3}})
        result = self.handle(engine, {'op': 'set', 'key': 'side', 'values': {'12': 30}})
        self.assertEqual(result['_apply'], [('side', {'12': 30})])
        engine.maxes = {'side': {'12': 50}}
        with patch.object(self.service, 'ddc') as ddc:
            engine.apply_now(result['_apply'])
        ddc.assert_called_once_with(4, '--sleep-multiplier', '.2', 'setvcp', '12', 15, '--noverify')

    def test_contrast_quirk_keeps_a_broken_monitor_inside_its_menu(self):
        engine = self.engine({'monitors': {}})
        engine.devices = {'main': {'bus': 5}, 'side': {'bus': 4}}
        quirks = {'ambient': {'main_monitor': 'main'},
                  'quirks': {'side': {'12': {'max': 22, 'auto': 0, 'shown_offset': 78}}}}
        with patch.object(self.service, 'load', return_value=quirks), patch.object(self.service, 'save'):
            result = engine.handle({'op': 'set', 'key': 'side', 'values': {'12': 100}})
            other = engine.handle({'op': 'set', 'key': 'main', 'values': {'12': 100}})
        self.assertEqual(result['_apply'], [('side', {'12': 22})])
        self.assertEqual(other['_apply'], [('main', {'12': 100})])
        self.assertEqual(engine.settings['monitors']['side']['values'], {'12': 22})

    def test_round_with_old_targets_never_overwrites_a_newer_request(self):
        with patch.object(self.service, 'ddc', return_value='VCP 10 C 20 100\nVCP 12 C 20 100\n') as ddc:
            engine = self.service.Engine()
            key, result = engine.apply_monitor(('main', {'bus': 1, 'model': 'test', 'name': 'DP-0'}),
                                               {'10': 50, '12': 50, 'mode': 'movie'}, True, stale=lambda: True)
        self.assertEqual(ddc.call_count, 1, 'read only, no write')
        self.assertFalse(result['settled'])

    def test_shadows_setting_validated_and_migrated_from_gamma(self):
        engine = self.engine({'monitors': {}, 'gamma': 1.4})
        self.assertEqual(engine.settings['shadows'], 40)
        self.assertNotIn('gamma', engine.settings)
        self.assertEqual(self.engine({'monitors': {}})
                         .settings['shadows'], self.service.DEFAULT_SHADOWS)
        self.handle(engine, {'op': 'shadows', 'value': 65})
        self.assertEqual(engine.settings['shadows'], 65)
        for bad in (float('nan'), 101, -1, '50', None):
            with self.assertRaises(ValueError):
                self.handle(engine, {'op': 'shadows', 'value': bad})

    def test_failed_hardware_readback_is_not_success(self):
        with patch.object(self.service, 'ddc', return_value='VCP 10 C 20 100\nVCP 12 C 20 100\n'):
            engine = self.service.Engine()
            with self.assertRaisesRegex(RuntimeError, 'readback differs'):
                engine.apply_monitor(('main', {'bus': 1, 'model': 'test'}),
                                     {'10': 50, '12': 50, 'mode': 'movie'}, True)


if __name__ == '__main__':
    unittest.main()
