"""Isolated GTK smoke test: fake monitors, no physical display writes, no service."""
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
module = runpy.run_path(str(root / 'monitor-panel'), run_name='panel_ui_test')
Panel, Bus, Gtk, GLib, Gdk = [module[n] for n in ('Panel', 'Bus', 'Gtk', 'GLib', 'Gdk')]
calls = []
temporary = tempfile.TemporaryDirectory()
config_path = Path(temporary.name) / 'config.json'
status_path = Path(temporary.name) / 'status.json'
main_key = 'GSM-5ae4-01010101'
side_key = 'HKC-0000-01010101'
config_path.write_text(json.dumps({'ambient': {'enabled': True, 'main_monitor': main_key},
    'profiles': [{'name': 'Movie', 'mode': 'movie'}, {'name': 'Game', 'mode': 'game'}],
    'names': {main_key: 'Main LG', side_key: 'Side'}}))
status_path.write_text(json.dumps({'ready': True, 'updated_at': time.time(), 'level': 12, 'level_exact': 12.3,
    'levels': {'Night': 5, 'Evening': 10, 'Dusk': 15, 'Day': 20},
    'light': {'source': 'Light sensor', 'lux': 21.8}, 'main_monitor': main_key,
    'shadows': 40, 'match': {side_key: [[10, 0]]},
    'overrides': {main_key: {'mode': 'game'}, side_key: {'mode': 'manual', 'values': {'10': 0}}},
    'monitors': {main_key: {'values': {'10': 12, '12': 20}, 'settled': True},
                 side_key: {'values': {'10': 0, '12': 20}, 'settled': True}}}))
Panel.__init__.__globals__['CONFIG'] = str(config_path)
Panel.__init__.__globals__['STATUS'] = status_path

writes = []
Bus.write = lambda self, feature, value: writes.append((self.n, feature, value))   # never reaches ddcutil
Bus.read = lambda self, features, done: done({'10': 10, '12': 20, '62': 40, '8D': 2}, {'10': 100, '12': 100})


def detect(self, force=False):
    main = self.config['ambient']['main_monitor']
    self.populate({main: {'bus': 5, 'model': 'Main LG'}, side_key: {'bus': 4, 'model': 'Side'}}, [
        {'name': 'DP-0', 'key': main, 'model': 'Main LG', 'x': 0, 'y': 0, 'w': 2560, 'h': 1080},
        {'name': 'HDMI-0', 'key': side_key, 'model': 'Side', 'x': 2560, 'y': 0, 'w': 1080, 'h': 1920}])


def control(self, payload, after=None):       # the service is not involved
    calls.append(payload)
    if after:
        after()


Panel.detect = detect
Panel.control = control
# Xvfb has no window manager: focus follows the pointer, so the popup would close itself
# as soon as it is placed away from the pointer. Only the test closes it.
really_close, Panel.close = Panel.close, lambda self: None
panel = Panel()


class Event:
    button, direction = 1, Gdk.ScrollDirection.UP

    def __init__(self, x, y):
        self.x, self.y = x, y


def check():
    try:
        stops, pos_of, notch = module['stops'], module['pos_of'], module['notch']
        series = stops()
        assert series == [0, 1, 2, 3, 4, 5, 6, 8, 10, 13, 16, 20, 25, 32, 40, 50, 63, 80, 100]
        assert stops(255)[-4:] == [125, 160, 200, 255]
        assert series[notch(pos_of(15, series), 1, series)] == 16     # from between notches: the next one
        assert series[notch(pos_of(15, series), -1, series)] == 13

        assert list(panel.mode_btns) == ['auto', 'movie', 'game'] and not panel.chips
        for mode in ('movie', 'game', 'auto'):
            panel.mode_btns[mode].clicked()
        assert calls == [{'op': 'mode', 'mode': m} for m in ('movie', 'game', 'auto')], calls
        assert panel.mode_btns['game'].get_style_context().has_class('mp-on')
        assert panel.game_box.get_reveal_child() and panel.shadow_scale.get_level() == 40
        assert 'the light, 12%' in panel.ambient_label.get_text(), panel.ambient_label.get_text()

        main, side = panel.screens
        assert panel.selected is main and (main.values['10'], side.values['10']) == (12, 0)
        assert side.pinned and not main.pinned
        # Side is remembered as 0 looking like 10 on main: its automatic line sits at 2 for 12.
        assert (main.target, side.target) == (12, 2), (main.target, side.target)
        assert set(panel.controls) == {'12'}, panel.controls       # no colour, input or RGB anywhere

        # The desk is the control: drag the main screen up by two notches' worth of pixels.
        desk = panel.map
        x, y, w, h = desk.rect(main.mon)
        desk.pressed(desk, Event(x + w / 2, y + h / 2))
        desk.moved(desk, Event(x + w / 2, y + h / 2 - 2 * desk.PX_PER_NOTCH))
        assert main.values['10'] == 16, main.values     # 12 → 13 → 16: lands on notches
        desk.released(desk, Event(x + w / 2, y + h / 2 - 14))
        assert calls[-1] == {'op': 'set', 'key': main_key, 'values': {'10': 16}}, calls[-1]
        assert not writes, 'the service is the only writer of brightness'
        # Scroll over the side screen: one notch up; clicking it selects it.
        x, y, w, h = desk.rect(side.mon)
        desk.scrolled(desk, Event(x + w / 2, y + h / 2))
        assert calls[-1] == {'op': 'set', 'key': side_key, 'values': {'10': 1}}, calls[-1]
        desk.pressed(desk, Event(x + w / 2, y + h / 2))
        desk.released(desk, Event(x + w / 2, y + h / 2))
        assert panel.selected is side and panel.pin_btn.get_visible()
        panel.pin_btn.clicked()
        assert calls[-1] == {'op': 'release', 'key': side_key}
        panel.controls['12'].set_level(32)
        assert calls[-1] == {'op': 'set', 'key': side_key, 'values': {'12': 32}}, calls[-1]

        # All speaks the main monitor's scale; the side screen gets what looks the same.
        panel.all_scale.set_level(20)
        assert calls[-1] == {'op': 'levelset', 'monitors': {main_key: 20, side_key: 10}}, calls[-1]
        panel.match_btn.clicked()
        assert calls[-1] == {'op': 'match', 'level': 20, 'points': {side_key: 10}}, calls[-1]

        # The day curve's numbers: raising Night past Evening pushes Evening along.
        panel.scrubs['Night'].change(12)
        assert calls[-1] == {'op': 'levels', 'values': {'Night': 12, 'Evening': 12, 'Dusk': 15, 'Day': 20}}
        assert panel.scrubs['Evening'].value == 12
        panel.scrubs['Night'].change(5)
        panel.scrubs['Evening'].change(10)
        panel.shadow_scale.set_level(63)
        assert calls[-1] == {'op': 'shadows', 'value': 63}
        panel.select(main)

        def finish():
            try:
                win = panel.win.get_window()
                pix = Gdk.pixbuf_get_from_window(win, 0, 0, panel.win.get_allocated_width(),
                                                 panel.win.get_allocated_height())
                pix.savev(os.environ.get('PANEL_PREVIEW', '/tmp/monitor-panel-preview.png'), 'png', [], [])
                print(json.dumps({'ui': 'passed', 'requests': len(calls), 'size': [pix.get_width(), pix.get_height()]}))
            except Exception:
                import traceback
                traceback.print_exc()
                sys.stdout.flush()
                os._exit(1)
            really_close(panel)
            panel.quit()
            return False
        GLib.timeout_add(500, finish)
    except Exception:
        import traceback
        traceback.print_exc()
        os._exit(1)
    return False


GLib.timeout_add(1200, check)
panel.run(None)
temporary.cleanup()
