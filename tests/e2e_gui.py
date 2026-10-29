"""GUI end-to-end test (Playwright + WebKit).

Flow: create a project → import via "+ Add files" and a Finder drop → tighten → copy a selection to a
new track → multi-track editor (up to 33 tracks): scrub inside the trim range without moving it, resize only from the edges,
the playhead can't leave the trim range, I/O trim, speed, preview, replace a track, resize a track,
close tracks → timeline: drag in, reorder, edit a clip from its card and update it, play with a
moving cursor, seek on the ruler → enhance → A/B
original ↔ enhanced → "already enhanced" warning → join & export → timeline survives
a reload → Show in Finder → record (3-2-1, level meter, named take, cancel) → move to Trash → project isolation →
rename → delete the project.

The microphone is never used: recording runs on a generated tone (SOUNDLAB_RECORD_INPUT). The test
creates its own temporary project, generates audio with `say`, imports it through the GUI and
deletes the project at the end; existing projects are not touched. "Delete" goes to a temporary
folder instead of the real Trash, "Show in Finder" is a no-op, and previews play muted (?mute=1).

Usage:  .venv/bin/python tests/e2e_gui.py [--headed] [--shots DIR]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from array import array
from pathlib import Path

from playwright.sync_api import Locator, Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_fixtures(folder: Path) -> list[Path]:
    texts = {"e2e_a": "This is the first test clip for the sound lab editor.",
             "e2e_b": "And this is the second clip [[slnc 1200]] that will be joined.",  # a long pause
             "e2e_c": "This one is dropped in [[slnc 1000]] from Finder."}
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in texts.items():
            aiff = Path(tmp) / f"{name}.aiff"
            subprocess.run(["say", "-o", str(aiff), text], check=True)
            wav = folder / f"{name}.wav"
            # e2e_c gets 0.8 s of silence before (with a 5 ms click in it, like a mouse click) and 0.6 s after;
            # with the 1 s pause inside it's the Tighten fixture
            shape = (["-f", "lavfi", "-i", "aevalsrc='0.5*sin(2*PI*3000*t)*between(t,0.2,0.205)':s=22050:d=0.5",
                      "-filter_complex", "[0]adelay=800:all=1,apad=pad_dur=0.6[v];[v][1]amix=inputs=2:duration=first:normalize=0"]
                     if name == "e2e_c" else [])
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(aiff), *shape,
                            "-ar", "44100", "-c:a", "pcm_s24le", str(wav)], check=True)
            out.append(wav)
    return out


def duration(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(p)], capture_output=True, text=True, check=True)
    return float(r.stdout)


def energy(p: Path, start: float = 0.0) -> float:
    """Sum of squared samples (48 kHz mono) from `start`: cutting silence barely changes it, cutting
    speech does."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(p), "-ar", "48000", "-ac", "1",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return sum(x * x for x in array("f", raw))


def samples(p: Path, rate: int = 48000) -> array:
    """Mono float samples of p at `rate`."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-ar", str(rate), "-ac", "1", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return array("f", raw)


def parse_time(s: str) -> float:
    m, sec = s.split(":")
    return int(m) * 60 + float(sec)


def toast_path(page: Page, prefix: str) -> Path:
    toast = page.locator("#toast")
    # a hidden toast still holds the previous message: wait for the new one to show
    expect(toast).to_be_visible(timeout=60_000)
    expect(toast).to_contain_text(prefix, timeout=60_000)
    m = re.search(r"(projects/[^·\n]+?\.(?:m4a|wav))", toast.inner_text())  # names may have spaces
    assert m, f"no path in toast: {toast.inner_text()}"
    hide_toast(page)
    return ROOT / m.group(1)


def hide_toast(page: Page) -> None:
    page.evaluate("document.getElementById('toast').classList.add('hidden')")


def set_speed(lane: Locator, value: str) -> None:
    lane.locator(".lane-speed").evaluate(
        "(s, v) => { s.value = v; s.dispatchEvent(new Event('input', { bubbles: true })); }", value)


def lane_times(lane: Locator) -> tuple[float, float]:
    return parse_time(lane.locator(".lane-in-time").inner_text()), parse_time(lane.locator(".lane-out-time").inner_text())


def mouse_drag(page: Page, x0: float, y0: float, x1: float, y1: float) -> None:
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move((x0 + x1) / 2, (y0 + y1) / 2, steps=5)
    page.mouse.move(x1, y1, steps=5)
    page.mouse.up()
    # wavesurfer swallows clicks for 10 ms after a drag (so a drag's end isn't a click); a person
    # never acts that fast, so wait like one would
    page.wait_for_timeout(150)


def cursor_frac(lane: Locator) -> float:
    """Playhead position (0–1) read from wavesurfer's cursor in its shadow DOM."""
    left = lane.locator(".lane-wave > div").evaluate(
        "(host) => host.shadowRoot.querySelector('.cursor').style.left")
    return float(left.rstrip("%")) / 100


def step(msg: str) -> None:
    print(f"  ✓ {msg}", flush=True)


def run(page: Page, base: str, shots: Path, fixtures: list[Path], trash: Path,
        project: str, original: str) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))

    page.goto(base + "/?mute=1")
    expect(page.locator("#project-select")).to_have_value(original)
    expect(page.locator("#preset")).to_have_value("flat")
    expect(page.locator("#new-project")).to_have_text("+ New project")
    expect(page.locator("#export")).to_have_text("Join & Export")

    # New project: empty media, exports collapsed
    page.once("dialog", lambda d: d.accept(project))
    page.locator("#new-project").click()
    expect(page.locator("#project-select")).to_have_value(project)
    expect(page.locator(".group-title").first).to_contain_text("Media (0)")
    expect(page.locator(".lib-empty")).to_be_visible()
    expect(page.locator(".group-title.collapsible")).to_contain_text("▸ Exports")
    assert all((ROOT / "projects" / project / d).is_dir() for d in ("inputs", "outputs", "voice-records"))
    step(f"project created: {project} · UI in English")

    # Import with "+ Add files"
    with page.expect_response("**/api/import") as resp:
        page.locator("#file-input").set_input_files([str(fixtures[0]), str(fixtures[1])])
    imported = resp.value.json()["imported"]
    assert len(imported) == 2 and all(x.startswith(f"projects/{project}/inputs/") for x in imported), imported
    name_a, name_b = (Path(x).name for x in imported)
    item_a = page.locator(".lib-item", has_text=name_a)
    item_b = page.locator(".lib-item", has_text=name_b)
    expect(item_a).to_be_visible()
    expect(item_b).to_be_visible()
    step("imported with + Add files")

    # Unsupported files are rejected
    bad = fixtures[0].with_name("notes.txt")
    bad.write_text("not audio")
    page.locator("#file-input").set_input_files(str(bad))
    expect(page.locator("#toast")).to_contain_text("Import failed")
    # WebKit logs the expected 400 response as a console error; ignore only that
    errors[:] = [e for e in errors if "status of 400" not in e]
    step("unsupported file rejected")

    # Finder drop onto the editor → imported and opened as track 1
    lanes = page.locator(".lane")
    b64 = base64.b64encode(fixtures[2].read_bytes()).decode()
    with page.expect_response("**/api/import") as resp:
        page.evaluate("""({b64, name}) => {
            const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
            const dt = new DataTransfer();
            dt.items.add(new File([bytes], name, { type: "audio/wav" }));
            const el = document.getElementById("editor-drop");
            for (const type of ["dragover", "drop"])
                el.dispatchEvent(new DragEvent(type, { dataTransfer: dt, bubbles: true, cancelable: true }));
        }""", {"b64": b64, "name": fixtures[2].name})
    dropped = resp.value.json()["imported"][0]
    expect(lanes).to_have_count(1)
    expect(lanes.nth(0).locator(".lane-title")).to_have_text(dropped)
    expect(lanes.nth(0)).to_have_attribute("data-ready", "true", timeout=10_000)
    step("Finder drop imported and opened as track 1")

    # Tighten: the track stays as it is and the new "_tight" version opens right below it. Slider full
    # right (1 s): the pause inside (~1.2 s with the word decays) stays about 1 s, the ends go (0.8 s
    # with a click before, 0.6 s after)
    lane1 = lanes.nth(0)
    full_c = lane_times(lane1)[1]
    set_pause = lambda lane, v: lane.locator(".lane-pause").evaluate(  # noqa: E731
        "(s, v) => { s.value = v; s.dispatchEvent(new Event('input', { bubbles: true })); }", v)
    set_pause(lane1, "1")
    expect(lane1.locator(".lane-pause-val")).to_have_text("1.00 s")
    lane1.locator(".lane-tighten").click()
    gentle = toast_path(page, "Tightened")
    expect(lanes).to_have_count(2)
    expect(lane1.locator(".lane-title")).to_have_text(dropped)  # untouched
    expect(lanes.nth(1).locator(".lane-title")).to_have_text(str(gentle.relative_to(ROOT)))
    rep_g = json.loads(gentle.with_suffix(".json").read_text())
    assert rep_g["max_pause_sec"] == 1.0, rep_g
    # Full left = Skip silence (jump cuts): the pause goes down to 0.05 s, and this version opens right below
    # track 1 (above the first one)
    set_pause(lane1, "0.05")
    expect(lane1.locator(".lane-pause-val")).to_have_text("skip")
    lane1.locator(".lane-tighten").click()
    tight = toast_path(page, "Tightened")
    expect(lanes).to_have_count(3)
    expect(lane1.locator(".lane-title")).to_have_text(dropped)
    expect(lanes.nth(1).locator(".lane-title")).to_have_text(str(tight.relative_to(ROOT)))
    expect(lanes.nth(2).locator(".lane-title")).to_have_text(str(gentle.relative_to(ROOT)))
    expect(lanes.nth(1)).to_have_attribute("data-ready", "true", timeout=10_000)
    report = json.loads(tight.with_suffix(".json").read_text())
    removed = duration(fixtures[2]) - duration(tight)
    assert tight.parent.name == "inputs" and report["pauses_shortened"] == 1, report
    assert 1.6 < removed < 2.8 and duration(tight) < duration(gentle) - 0.6, (removed, duration(gentle))
    kept = energy(tight) / energy(fixtures[2], start=0.75)  # the reference starts after the click
    assert kept > 0.995, kept  # only silence / room tone went: no syllable was cut
    assert 0.55 <= report["trimmed_sec"][0] <= 0.8 and report["trimmed_sec"][1] >= 0.3, report["trimmed_sec"]
    assert abs(lane_times(lanes.nth(1))[1] - duration(tight)) < 0.1
    expect(page.locator(".lib-item", has_text=fixtures[2].name)).to_be_visible()  # the original stays
    page.screenshot(path=str(shots / "1_tighten.png"))
    lanes.nth(1).locator(".lane-tighten").click()  # its slider starts where the last one was left
    expect(page.locator("#toast")).to_contain_text("Nothing to tighten")
    hide_toast(page)
    lanes.nth(2).locator(".lane-close").click()
    lanes.nth(1).locator(".lane-close").click()
    expect(lanes).to_have_count(1)
    step(f"Tighten: track kept, new versions open below it — 1 s slider: {duration(gentle):.1f}s (pause ~1 s), "
         f"Skip silence: {full_c:.1f}s → {duration(tight):.1f}s (ends {report['trimmed_sec']}, pause cut, click "
         f"ignored, {100 * kept:.2f}% of the energy kept); again: nothing to tighten")

    # ▲ under the start / end time sets it to the playhead; an end before the start is ignored. The
    # playhead shows its exact time (ms)
    box = lane1.locator(".lane-wave").bounding_box()
    page.mouse.click(box["x"] + box["width"] * 0.1, box["y"] + box["height"] / 2)
    page.wait_for_timeout(150)
    p_in = lane1.locator(".lane-pos").inner_text()
    lane1.locator(".lane-set-in").click()
    expect(lane1.locator(".lane-in-time")).to_have_text(p_in)
    lane1.locator(".lane-set-out").click()  # the playhead is on the start now: an empty range, ignored
    assert lane_times(lane1) == (parse_time(p_in), full_c), lane_times(lane1)
    page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] / 2)
    page.wait_for_timeout(150)
    pos = lane1.locator(".lane-pos").inner_text()
    assert re.fullmatch(r"\d:\d\d\.\d{3}", pos) and abs(parse_time(pos) - full_c / 2) < 0.15, (pos, full_c)
    lane1.locator(".lane-set-out").click()
    expect(lane1.locator(".lane-out-time")).to_have_text(pos)
    step(f"▲ set the start / end to the playhead: {p_in} → {pos}; an end before the start ignored")

    # The track's chip dropped into the editor makes its selection a recording of its own (<name>_part.wav)
    # in a new track (the way to cut a take apart); dropped on its own track it does nothing
    lane1.locator(".lane-chip").drag_to(lane1.locator(".lane-wave"))
    expect(lanes).to_have_count(1)
    ed = page.locator("#editor-drop").bounding_box()
    empty = {"x": ed["width"] / 2, "y": ed["height"] - 12}
    assert not page.evaluate("([x, y]) => !!document.elementFromPoint(x, y).closest('.lane')",
                             [ed["x"] + empty["x"], ed["y"] + empty["y"]]), "no empty editor space"
    lane1.locator(".lane-chip").drag_to(page.locator("#editor-drop"), target_position=empty)
    part = toast_path(page, "New recording")
    expect(lanes).to_have_count(2)
    expect(lanes.nth(1).locator(".lane-title")).to_have_text(str(part.relative_to(ROOT)))
    assert part.name == Path(dropped).stem + "_part.wav", part
    expect(lanes.nth(1)).to_have_attribute("data-ready", "true", timeout=10_000)
    expect(lanes.nth(1).locator(".lane-in-time")).to_have_text("0:00.000")  # the whole file is the selection
    p0, p1 = parse_time(p_in), parse_time(pos)
    assert abs(lane_times(lanes.nth(1))[1] - (p1 - p0)) < 0.02, (lane_times(lanes.nth(1)), p0, p1)
    # Same samples as the source's range, past the 10 ms edge fades. Compared at the source's rate (the
    # fixtures are 44.1 kHz): atrim cuts on its sample grid, so a 48 kHz cut is off by a fraction of a sample
    got = samples(part, 44100)
    i0 = round(p0 * 44100)
    want = samples(ROOT / dropped, 44100)[i0:i0 + len(got)]
    assert abs(len(got) - round((p1 - p0) * 44100)) < 3 and len(want) == len(got), (len(got), p0, p1)
    assert max(abs(x - y) for x, y in zip(got[441:-441], want[441:-441])) < 1e-3
    rep = json.loads(part.with_suffix(".json").read_text())
    assert rep["part"] and all(abs(a - b) < 0.0015 for a, b in zip(rep["range"], (p0, p1))), (rep, p0, p1)
    # Onto another track: a new one right below it, nothing replaced; the speed carries over
    hide_toast(page)
    page.locator("#editor-drop .panel-head h2").click()  # see "Vertical resize": after a simulated drag
    set_speed(lanes.nth(1), "1.5")
    lanes.nth(1).locator(".lane-chip").drag_to(lane1.locator(".lane-wave"))
    part2 = toast_path(page, "New recording")
    assert part2.name == Path(dropped).stem + "_part-2.wav", part2
    expect(lanes).to_have_count(3)
    expect(lanes.nth(1).locator(".lane-title")).to_have_text(str(part2.relative_to(ROOT)))  # below track 1
    expect(lanes.nth(1).locator(".lane-speed-val")).to_have_text("1.50×")
    expect(lane1.locator(".lane-speed-val")).to_have_text("1.00×")
    expect(lane1.locator(".lane-out-time")).to_have_text(pos)
    hide_toast(page)
    page.locator("#editor-drop .panel-head h2").click()
    for _ in range(2):
        lanes.nth(1).locator(".lane-close").click()
    expect(lanes).to_have_count(1)
    step(f"track chip dropped in the editor: {part.name} ({duration(part):.2f}s, same samples as the range) "
         f"at the end, {part2.name} below the track it lands on; on its own track ignored")

    # Arrow keys move the playhead in fixed steps (10 ms, ⇧ 100 ms, ⌥ 1 ms), inside the trim range
    page.mouse.click(box["x"] + box["width"] * 0.25, box["y"] + box["height"] / 2)
    page.wait_for_timeout(150)
    pos_ms = lambda: round(parse_time(lane1.locator(".lane-pos").inner_text()) * 1000)  # noqa: E731
    p0 = pos_ms()
    for key, delta in [("ArrowRight", 10), ("Shift+ArrowRight", 100), ("Alt+ArrowRight", 1), ("ArrowLeft", -10)]:
        before = pos_ms()
        page.keyboard.press(key)
        page.wait_for_timeout(50)
        assert pos_ms() - before == delta, (key, before, pos_ms())
    for _ in range(30):  # can't pass the end of the trim range
        page.keyboard.press("Shift+ArrowRight")
    expect(lane1.locator(".lane-pos")).to_have_text(lane1.locator(".lane-out-time").inner_text())

    # Zoom (per track): + / − / 0 keys, ⌘ + scroll, Fit; the ruler's labels get decimals when zoomed in
    zoom_val = lane1.locator(".lane-zoom-val")
    per_px = lambda: float(re.search(r"= ([\d.]+) ms", zoom_val.inner_text())[1])  # noqa: E731
    expect(zoom_val).to_contain_text("fit")
    fit_ms = per_px()
    scroll_w = lambda: lane1.locator(".lane-wave > div").evaluate(  # noqa: E731
        "(h) => { const s = h.shadowRoot.querySelector('[part~=\"scroll\"]'); return [s.scrollWidth, s.clientWidth]; }")
    for _ in range(3):
        page.keyboard.press("+")
    expect(zoom_val).not_to_contain_text("fit")
    want = max(1.0, fit_ms / 8)  # 3 × "+" doubles 3 times, up to the 1 px = 1 ms limit
    assert abs(per_px() - want) < max(0.15, want * 0.06), (per_px(), fit_ms)
    sw, cw = scroll_w()
    assert abs(sw / cw - fit_ms / want) < 0.3, (sw, cw, fit_ms / want)
    labels = lane1.locator(".lane-wave > div").evaluate(
        "(h) => h.shadowRoot.querySelector('[part~=\"timeline\"]').innerText")
    assert re.search(r"\d\.\d{2}", labels), labels  # e.g. "1.25": finer ticks than whole seconds
    page.screenshot(path=str(shots / "1_zoom.png"))
    page.keyboard.press("0")
    expect(zoom_val).to_contain_text("fit")
    wave = lane1.locator(".lane-wave").bounding_box()
    page.mouse.move(wave["x"] + wave["width"] / 2, wave["y"] + wave["height"] / 3)
    page.keyboard.down("Meta")
    page.mouse.wheel(0, -120)
    page.keyboard.up("Meta")
    expect(zoom_val).not_to_contain_text("fit")
    zoomed = per_px()
    lane1.locator(".lane-zoom-fit").click()
    expect(zoom_val).to_contain_text("fit")
    step(f"arrows: +10 / +100 / +1 / −10 ms from {p0} ms, stop at the range end; zoom: fit 1 px = {fit_ms} ms "
         f"→ 3× + = {want:.1f} ms (ruler with decimals), 0 = fit, ⌘+scroll = {zoomed} ms, Fit")

    # Drag from media onto the editor header (not onto a track) → new track 2
    item_a.drag_to(page.locator("#editor-drop .panel-head"))
    expect(lanes).to_have_count(2)
    lane = lanes.nth(1)
    expect(lane.locator(".lane-title")).to_have_text(imported[0])
    expect(lane).to_have_attribute("data-ready", "true", timeout=10_000)
    expect(lane).to_have_class(re.compile("active"))
    expect(page.locator("#track-count")).to_have_text("2/33")
    full = lane_times(lane)[1]
    assert abs(full - duration(fixtures[0])) < 0.15, full
    step(f"track 2 added by dragging ({full:.1f}s), counter 2/33")

    wave = lane.locator(".lane-wave").bounding_box()
    y = wave["y"] + wave["height"] / 2
    x_at = lambda frac: wave["x"] + wave["width"] * frac  # noqa: E731

    # Pressing and dragging inside the trim range scrubs the playhead; the range doesn't move
    mouse_drag(page, x_at(0.40), y, x_at(0.60), y)
    assert lane_times(lane) == (0.0, full), lane_times(lane)
    page.keyboard.press("i")
    t_in = lane_times(lane)[0]
    assert abs(t_in - full * 0.60) < 0.25, (t_in, full)
    step(f"drag inside the range scrubbed the playhead to {t_in:.1f}s, range not moved")

    # Only the edge handles resize the range
    handle = lane.locator('[part~="region-handle-right"]')
    hb = handle.bounding_box()
    mouse_drag(page, hb["x"] + hb["width"] / 2, y, x_at(0.85), y)
    t_in2, t_out = lane_times(lane)
    assert abs(t_out - full * 0.85) < 0.25 and abs(t_in2 - t_in) < 0.01, (t_in2, t_out)
    step(f"right edge dragged: end {full:.1f}s → {t_out:.1f}s")

    # Widening goes through the edges (the playhead can't leave the range, so I/O only narrow it)
    handle = lane.locator('[part~="region-handle-left"]')
    hb = handle.bounding_box()
    mouse_drag(page, hb["x"] + hb["width"] / 2, y, x_at(0.10), y)
    assert abs(lane_times(lane)[0] - full * 0.10) < 0.25, lane_times(lane)
    step(f"left edge dragged: start → {lane_times(lane)[0]:.1f}s")

    # I / O at the playhead (inside the range)
    page.mouse.click(x_at(0.20), y)
    page.keyboard.press("i")
    page.mouse.click(x_at(0.80), y)
    page.keyboard.press("o")
    t_in, t_out = lane_times(lane)
    assert abs(t_in - full * 0.2) < 0.4 and abs(t_out - full * 0.8) < 0.4, (t_in, t_out)
    step(f"I/O trim: {t_in:.1f}s → {t_out:.1f}s")

    # The playhead can't leave the trim range: a click left of it stops at the start,
    # a drag past its end stops at the end
    page.mouse.click(x_at(0.05), y)
    page.wait_for_timeout(150)
    assert abs(cursor_frac(lane) * full - t_in) < 0.1, (cursor_frac(lane) * full, t_in)
    mouse_drag(page, x_at(0.5), y, x_at(0.97), y)
    page.wait_for_timeout(300)  # wavesurfer applies a drag-seek after a short debounce
    assert abs(cursor_frac(lane) * full - t_out) < 0.1, (cursor_frac(lane) * full, t_out)
    step("playhead clamped to the trim range (click before it, drag past it)")

    set_speed(lane, "1.5")
    expect(lane.locator(".lane-speed-val")).to_have_text("1.50×")
    chip = parse_time(lane.locator(".lane-chip-dur").inner_text())
    assert abs(chip - (t_out - t_in) / 1.5) < 0.15, chip
    step(f"speed 1.5×, clip length {chip:.1f}s")

    lane.locator(".lane-play").click()
    expect(lane.locator(".lane-play")).to_contain_text("Pause")
    page.wait_for_timeout(300)
    page.keyboard.press("Space")
    expect(lane.locator(".lane-play")).to_contain_text("Play")
    step("preview play / pause (muted)")
    page.screenshot(path=str(shots / "1_editor.png"))

    # Track → timeline
    cards = page.locator(".card")
    lane.locator(".lane-chip").drag_to(page.locator("#timeline-drop"))
    expect(cards).to_have_count(1)
    expect(cards.nth(0)).to_contain_text("1.50×")
    item_b.drag_to(page.locator("#track"))
    expect(cards).to_have_count(2)
    expect(cards.nth(1)).to_contain_text(name_b)
    first = cards.nth(0).bounding_box()
    cards.nth(1).drag_to(cards.nth(0), target_position={"x": 5, "y": first["height"] / 2})
    expect(cards.nth(0)).to_contain_text(name_b)
    expect(cards.nth(1)).to_contain_text(name_a)
    step("track chip and media file dragged to the timeline, cards reordered")

    # Cards look like the editor (waveform drawn) and are as wide as their clips are long
    expect(page.locator('.card canvas[data-drawn="true"]')).to_have_count(2, timeout=10_000)
    lens = [parse_time(cards.nth(k).locator(".dur").inner_text()) for k in range(2)]
    widths = [cards.nth(k).bounding_box()["width"] for k in range(2)]
    # lengths on the cards are rounded to 0.1 s, so compare with a relative tolerance
    assert abs((widths[0] / widths[1]) / (lens[0] / lens[1]) - 1) < 0.12, (widths, lens)
    step(f"cards show waveforms, widths proportional ({widths[0]:.0f}px : {widths[1]:.0f}px ≈ {lens[0]:.1f}s : {lens[1]:.1f}s)")

    # Clicking a card opens it in a new track (3/3) in "update" mode
    cards.nth(1).click()
    expect(lanes).to_have_count(3)
    lane3 = lanes.nth(2)
    expect(lane3.locator(".lane-badge")).to_have_text("Editing timeline clip 2")
    expect(lane3.locator(".lane-add")).to_have_text("Update clip ✓")
    expect(lane3.locator(".lane-speed-val")).to_have_text("1.50×")
    expect(lane3).to_have_attribute("data-ready", "true", timeout=10_000)
    expect(cards.nth(1)).to_have_class(re.compile("editing"))
    set_speed(lane3, "1")
    lane3.locator(".lane-add").click()
    expect(cards.nth(1).locator(".meta")).not_to_contain_text("1.50×")
    expect(lane3.locator(".lane-add")).to_have_text("Add to timeline ↓")
    expect(lane3.locator(".lane-badge")).to_be_hidden()
    step("card opened in track 3, speed changed, Update clip wrote it back")

    # Up to 33 tracks; the 34th is refused
    for n in range(4, 34):
        item_b.dblclick()
        expect(page.locator("#track-count")).to_have_text(f"{n}/33")
    expect(lanes).to_have_count(33)
    item_b.dblclick()
    expect(page.locator("#toast")).to_contain_text("up to 33 recordings")
    expect(lanes).to_have_count(33)
    hide_toast(page)
    while lanes.count() > 3:
        lanes.last.locator(".lane-close").click()
    expect(page.locator("#track-count")).to_have_text("3/33")
    step("33 tracks opened, 34th refused, extra tracks closed")

    # Dropping onto a track replaces it
    item_b.drag_to(lanes.nth(0).locator(".lane-wave"))
    expect(lanes).to_have_count(3)
    expect(lanes.nth(0).locator(".lane-title")).to_have_text(imported[1])
    expect(lanes.nth(0)).to_have_attribute("data-ready", "true", timeout=10_000)
    step("dropping onto track 1 replaced its recording")

    # Vertical resize
    # Playwright's simulated HTML5 drag in WebKit swallows the next pointerdown (a real drag doesn't);
    # a neutral click first, as a person's next action would be anyway
    page.locator("#editor-drop .panel-head h2").click()
    h0 = lanes.nth(0).locator(".lane-wave").bounding_box()["height"]
    rb = lanes.nth(0).locator(".lane-resize").bounding_box()
    mouse_drag(page, rb["x"] + rb["width"] / 2, rb["y"] + rb["height"] / 2, rb["x"] + rb["width"] / 2, rb["y"] + 70)
    h1 = lanes.nth(0).locator(".lane-wave").bounding_box()["height"]
    assert 55 < h1 - h0 < 85, (h0, h1)
    rb = lanes.nth(0).locator(".lane-resize").bounding_box()
    mouse_drag(page, rb["x"] + rb["width"] / 2, rb["y"] + rb["height"] / 2, rb["x"] + rb["width"] / 2, rb["y"] - 400)
    h2 = lanes.nth(0).locator(".lane-wave").bounding_box()["height"]
    assert 74 <= h2 <= 90, h2  # clamped to the minimum (60 px waveform + the 20 px ruler)
    step(f"track resized: {h0:.0f}px → {h1:.0f}px → {h2:.0f}px (min)")
    page.screenshot(path=str(shots / "2_tracks.png"))

    lanes.nth(0).locator(".lane-close").click()
    expect(lanes).to_have_count(2)
    expect(page.locator("#track-count")).to_have_text("2/33")
    expect(lanes.nth(0).locator(".lane-num")).to_have_text("1")
    step("track closed, tracks renumbered")

    # Timeline playback: the cursor moves, seeking on the ruler, stops and rewinds at the end
    cursor = page.locator("#tl-cursor")
    total = parse_time(page.locator("#total").inner_text())
    x0 = cursor.bounding_box()["x"]
    page.locator("#tl-play").click()
    expect(page.locator("#tl-play")).to_contain_text("Pause", timeout=10_000)  # may render first
    page.wait_for_timeout(900)
    pos = parse_time(page.locator("#tl-pos").inner_text())
    assert 0.3 < pos < 1.6, pos
    assert cursor.bounding_box()["x"] > x0 + 20, (x0, cursor.bounding_box()["x"])
    page.locator("#tl-play").click()
    expect(page.locator("#tl-play")).to_contain_text("Play")
    step(f"timeline played, cursor moved, paused at {pos:.1f}s")

    seg2 = page.locator(".ruler-seg").nth(1).bounding_box()
    page.mouse.click(seg2["x"] + seg2["width"] / 2, seg2["y"] + seg2["height"] / 2)
    clip1 = parse_time(page.locator(".card").nth(0).locator(".dur").inner_text())
    clip2 = parse_time(page.locator(".card").nth(1).locator(".dur").inner_text())
    pos = parse_time(page.locator("#tl-pos").inner_text())
    assert abs(pos - (clip1 + clip2 / 2)) < 0.2, (pos, clip1, clip2)
    played = [float(page.locator(".card").nth(k).evaluate("(c) => c.style.getPropertyValue('--played')"))
              for k in range(2)]
    assert played[0] == 1 and abs(played[1] - 0.5) < 0.1, played
    step(f"ruler click moved the playhead into clip 2 ({pos:.1f}s), played parts tinted")

    # Headless WebKit's media clock sometimes jumps ahead (a bare <audio> page ends a 4.9 s file
    # after 1.4–4 s, now and then), so an early end gets one retry; a real early stop repeats
    retried = ""
    for attempt in range(2):
        page.locator(".ruler-seg").nth(0).click(position={"x": 2, "y": 6})  # back to the start
        started = time.monotonic()
        page.locator("#tl-play").click()  # plays both clips as one file, then rewinds
        expect(page.locator("#tl-play")).to_contain_text("Play", timeout=total * 1000 + 5000)
        took = time.monotonic() - started
        expect(page.locator("#tl-pos")).to_have_text("0:00.0")
        if took > total - 0.3 or attempt:
            break
        retried = f" (retried: WebKit ended after {took:.1f}s)"
    # + the time the preview takes to render if it isn't ready yet (seen up to ~1 s)
    assert total - 0.3 < took < total + 1.5, (took, total)
    step(f"played start to end in {took:.1f}s (timeline {total:.1f}s) as one file, rewound{retried}")

    # Enhance the active track
    lanes.nth(0).locator(".lane-enhance").click()
    enhanced = toast_path(page, "Enhanced")
    assert enhanced.is_file()
    expect(page.locator(".lib-item", has_text=enhanced.name)).to_be_visible()  # collapsed group opens
    expect(lanes).to_have_count(3)  # like Tighten: the enhanced version opens right below its track
    expect(lanes.nth(1).locator(".lane-title")).to_have_text(str(enhanced.relative_to(ROOT)))
    step(f"✨ Enhance: {enhanced.relative_to(ROOT)}, opened below its track")

    # A/B: an enhanced file in a track switches to its level-matched original at the same position
    expect(lanes.nth(0).locator(".lane-ab")).to_be_hidden()  # not an enhanced file
    lane_e = lanes.nth(1)
    expect(lane_e).to_have_attribute("data-ready", "true", timeout=10_000)
    ab = lane_e.locator(".lane-ab")
    expect(ab).to_have_text("B · Enhanced")
    box = lane_e.locator(".lane-wave").bounding_box()
    page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] / 2)
    page.wait_for_timeout(150)
    f0 = cursor_frac(lane_e)
    ab.click()
    expect(ab).to_have_text(re.compile(r"^A · Original( [+−]\d+ dB)?$"), timeout=15_000)
    expect(lane_e).to_have_attribute("data-ready", "true")
    f1 = cursor_frac(lane_e)
    assert abs(f0 - 0.5) < 0.05 and abs(f1 - f0) < 0.03, (f0, f1)
    resp_ab = page.evaluate("(p) => fetch('/api/original', { method: 'POST', headers: { 'Content-Type': "
                            "'application/json' }, body: JSON.stringify({ path: p }) }).then((r) => r.json())",
                            str(enhanced.relative_to(ROOT)))
    boost = resp_ab["gain"]
    label = "A · Original" + ("" if abs(boost) < 0.5 else f" {'+' if boost > 0 else '−'}{abs(boost):.0f} dB")
    expect(ab).to_have_text(label)  # the label says how much the original was raised to match
    orig_file = Path(tempfile.gettempdir()) / "soundlab-preview" / resp_ab["url"].rsplit("/", 1)[1]
    sys.path.insert(0, str(ROOT))
    from soundlab.pipeline import measure_loudness
    gap = measure_loudness(orig_file)["lufs"] - measure_loudness(enhanced)["lufs"]
    assert abs(duration(orig_file) - duration(enhanced)) < 0.1 and abs(gap) < 0.5, (gap, orig_file)
    page.keyboard.press("b")
    expect(ab).to_have_text("B · Enhanced")
    expect(lane_e).to_have_attribute("data-ready", "true")
    assert abs(cursor_frac(lane_e) - f0) < 0.03
    page.screenshot(path=str(shots / "3_ab.png"))
    lane_e.locator(".lane-close").click()
    expect(lanes).to_have_count(2)
    step(f"A/B: '{label}' (level-matched, {gap:+.1f} LU) ↔ enhanced at the same position ({f0:.2f})")

    # Adding an already-enhanced file to the timeline shows a warning while Enhance is ticked
    page.locator(".lib-item", has_text=enhanced.name).drag_to(page.locator("#track"))
    expect(page.locator("#enhance-warn")).to_have_text("⚠ 1 clip is already enhanced")
    page.locator("#enhance").uncheck()
    expect(page.locator("#enhance-warn")).to_be_hidden()
    page.locator("#enhance").check()
    page.locator(".card", has_text=enhanced.name).locator(".remove").click()
    expect(page.locator(".card")).to_have_count(2)
    expect(page.locator("#enhance-warn")).to_be_hidden()
    step("'already enhanced' warning shown / hidden")

    # Join & export — expected length = sum of cards
    expected = parse_time(page.locator("#total").inner_text())
    page.locator("#export").click()
    exported = toast_path(page, "Exported")
    got = duration(exported)
    assert abs(got - expected) < 0.25, (got, expected)
    step(f"exported: {exported.relative_to(ROOT)} ({got:.2f}s, expected {expected:.2f}s)")
    page.screenshot(path=str(shots / "3_exported.png"))

    # Timeline survives a reload; removing a card works
    page.reload()
    expect(page.locator(".card")).to_have_count(2)
    expect(lanes).to_have_count(0)
    page.locator(".card").nth(0).locator(".remove").click()
    expect(page.locator(".card")).to_have_count(1)
    step("timeline kept after reload, card removed")

    item_a.hover()
    with page.expect_response("**/api/reveal") as resp:
        item_a.locator('[data-act="reveal"]').click()
    assert resp.value.ok
    step("Show in Finder called")

    # Recording (a generated tone stands in for the mic): 3-2-1, named take, countdown cut from the file
    rec = page.locator("#rec-btn")
    page.locator("#take-name").fill("e2e take")
    expect(page.locator("#countdown")).to_be_checked()
    rec.click()
    expect(page.locator("#rec-label")).to_have_text("3")
    expect(page.locator("#take-name")).to_be_disabled()
    # Live level meter, already during the countdown: the test tone peaks at -18 dBFS
    expect(page.locator("#rec-meter")).to_have_attribute("data-level", re.compile(r"^-1[78]\.\d$"), timeout=3000)
    page.screenshot(path=str(shots / "4_recording.png"))
    expect(page.locator("#rec-label")).to_have_text("Stop", timeout=5000)
    page.wait_for_timeout(1500)
    rec.click()
    take = toast_path(page, "Saved")
    assert take.name == "e2e take.wav" and take.parent.parent.name == "voice-records", take
    assert 1.4 < duration(take) < 2.8, duration(take)  # 1.5 s after "go" + 0.3 s lead-in + stop latency
    expect(lanes.last.locator(".lane-title")).to_have_text(str(take.relative_to(ROOT)))
    lanes.last.locator(".lane-close").click()
    # Cancelled during the countdown: nothing kept, the file goes to the Trash
    rec.click()
    expect(page.locator("#rec-label")).to_have_text("3")
    rec.click()
    expect(page.locator("#toast")).to_contain_text("Recording cancelled")
    expect(page.locator("#rec-label")).to_have_text("Record")
    expect(page.locator("#rec-meter")).to_be_hidden()
    assert (trash / "e2e take-2.wav").is_file() and not (take.parent / "e2e take-2.wav").exists()
    page.locator("#take-name").fill("")
    hide_toast(page)
    step(f"recorded with 3-2-1 and a live level meter as '{take.name}' ({duration(take):.1f}s, countdown cut), "
         "cancelled countdown trashed")

    # Move to Trash: file moved, clips using it leave the timeline
    expect(page.locator(".card").first).to_contain_text(name_a)
    page.once("dialog", lambda d: d.accept())
    item_a.hover()
    item_a.locator('[data-act="trash"]').click()
    expect(page.locator("#toast")).to_contain_text("Moved to the Trash")
    expect(item_a).to_have_count(0)
    expect(page.locator(".card")).to_have_count(0)
    assert not (ROOT / imported[0]).exists() and (trash / name_a).is_file()
    step("moved to the Trash, its clip left the timeline")

    # Project isolation
    item_b.drag_to(page.locator("#track"))
    expect(page.locator(".card")).to_have_count(1)
    page.locator("#project-select").select_option(original)
    expect(item_b).to_have_count(0)
    expect(page.locator(".card", has_text=name_b)).to_have_count(0)
    page.locator("#project-select").select_option(project)
    expect(item_b).to_be_visible()
    expect(page.locator(".card", has_text=name_b)).to_have_count(1)
    step("switching projects: media and timeline are per project")

    # Rename: folder moves, the saved timeline follows with its paths rewritten
    renamed = f"{project}-renamed"
    page.once("dialog", lambda d: d.accept(renamed))
    page.locator("#rename-project").click()
    expect(page.locator("#toast")).to_contain_text(f"{project} → {renamed}")
    expect(page.locator("#project-select")).to_have_value(renamed)
    expect(page.locator(f'#project-select option[value="{project}"]')).to_have_count(0)
    assert not (ROOT / "projects" / project).exists() and (ROOT / "projects" / renamed).is_dir()
    assert (ROOT / "projects" / ".current").read_text().strip() == renamed
    expect(item_b).to_be_visible()
    expect(page.locator(".card", has_text=name_b)).to_have_count(1)
    saved = page.evaluate(f"""() => [localStorage.getItem("soundlab.timeline.{project}"),
                                     JSON.parse(localStorage.getItem("soundlab.timeline.{renamed}"))]""")
    assert saved[0] is None and saved[1][0]["path"].startswith(f"projects/{renamed}/"), saved
    page.locator("#tl-play").click()  # the preview render resolves the rewritten paths on the server
    expect(page.locator("#tl-play")).to_contain_text("Pause", timeout=10_000)
    page.locator("#tl-play").click()
    expect(page.locator("#tl-play")).to_contain_text("Play")
    page.screenshot(path=str(shots / "5_renamed.png"))
    step(f"project renamed to {renamed}: media and timeline carried over")

    # Delete the project
    project = renamed
    page.once("dialog", lambda d: d.accept())
    page.locator("#delete-project").click()
    expect(page.locator("#toast")).to_contain_text("moved to the Trash")
    expect(page.locator(f'#project-select option[value="{project}"]')).to_have_count(0)
    assert not (ROOT / "projects" / project).exists() and (trash / project).is_dir()
    step("project deleted (moved to the Trash)")

    assert not errors, "\n".join(errors)
    step("no console errors")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--shots", type=Path, default=Path(tempfile.gettempdir()) / "soundlab-e2e")
    args = ap.parse_args()
    args.shots.mkdir(parents=True, exist_ok=True)

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    work = Path(tempfile.mkdtemp(prefix="soundlab-e2e-"))
    trash = work / "trash"
    fixtures = make_fixtures(work)
    env = {**os.environ, "SOUNDLAB_TRASH": str(trash), "SOUNDLAB_REVEAL_CMD": "true",
           # never the real microphone: a generated tone, in real time
           "SOUNDLAB_RECORD_INPUT": "tone:330"}
    project = f"e2e-{os.getpid()}"
    current_file = ROOT / "projects" / ".current"
    original = current_file.read_text().strip() if current_file.exists() else "Default"
    server = subprocess.Popen([str(PY), "-m", "soundlab", "gui", "--no-browser", "--port", str(port)],
                              cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/config", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        print(f"GUI e2e (WebKit) → {base}")
        with sync_playwright() as p:
            browser = p.webkit.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            try:
                run(page, base, args.shots, fixtures, trash, project, original)
            except Exception:
                page.screenshot(path=str(args.shots / "failure.png"))
                raise
            finally:
                browser.close()
        print(f"PASSED · screenshots: {args.shots}")
        return 0
    finally:
        server.terminate()
        server.wait()
        shutil.rmtree(work, ignore_errors=True)
        # If the test stopped halfway, remove its project and restore the current project
        for name in (project, f"{project}-renamed"):
            shutil.rmtree(ROOT / "projects" / name, ignore_errors=True)
        current_file.write_text(original)


if __name__ == "__main__":
    sys.exit(main())
