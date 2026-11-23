#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DVS-Streamtest ohne Oberfläche – derselbe Python-Weg wie in der App
(initiate_device -> EventsIterator -> PeriodicFrameGenerationAlgorithm).

Gibt jede Sekunde Events/s, Pakete/s, Vorschaubilder/s und die Rechenzeit pro
Paket aus und meldet, wenn der Datenstrom hängt.

    ~/straiver-venv/bin/python -u tools/dvs_stream_test.py                    # nur DVS
    ~/straiver-venv/bin/python -u tools/dvs_stream_test.py --webcam /dev/video2  # DVS + Webcam parallel
"""
import argparse
import os
import threading
import time

import cv2
from metavision_core.event_io import EventsIterator
from metavision_core.event_io.raw_reader import initiate_device
from metavision_sdk_core import ColorPalette, PeriodicFrameGenerationAlgorithm

ap = argparse.ArgumentParser()
ap.add_argument("--seconds", type=float, default=60)
ap.add_argument("--webcam", default="", help="z. B. /dev/video2 – streamt parallel die Webcam")
ap.add_argument("--no-frames", action="store_true", help="ohne Vorschaubild-Erzeugung")
args = ap.parse_args()

stats = {"ev": 0, "batches": 0, "frames": 0, "cam": 0, "proc": 0.0, "last_batch": time.monotonic()}
stop = threading.Event()


def webcam_loop(path):
    cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    cap.set(cv2.CAP_PROP_FPS, 30)
    print(f"[Webcam] geöffnet: {cap.isOpened()}", flush=True)
    while not stop.is_set():
        ok, _ = cap.read()
        if ok:
            stats["cam"] += 1
        else:
            time.sleep(0.05)
    cap.release()


def monitor():
    t0 = time.monotonic()
    prev = dict(stats)
    hung_reported = False
    while not stop.is_set():
        time.sleep(1.0)
        now = time.monotonic()
        d = {k: stats[k] - prev[k] for k in ("ev", "batches", "frames", "cam")}
        proc_ms = (stats["proc"] - prev["proc"]) / max(d["batches"], 1) * 1000
        prev = dict(stats)
        idle = now - stats["last_batch"]
        line = (f"{now - t0:5.0f}s  {d['ev'] / 1e6:6.2f} Mev/s  {d['batches']:4d} Pakete/s  "
                f"{d['frames']:3d} Bilder/s  {proc_ms:6.1f} ms/Paket")
        if args.webcam:
            line += f"  Webcam {d['cam']:3d} fps"
        if idle > 2.0:
            line += f"  <<< HÄNGT seit {idle:.0f} s"
            if idle > 15 and not hung_reported:
                hung_reported = True
                print(line, flush=True)
                print("ERGEBNIS: Datenstrom hängt (kein Paket seit 15 s) – Abbruch.", flush=True)
                os._exit(2)
        print(line, flush=True)


if args.webcam:
    threading.Thread(target=webcam_loop, args=(args.webcam,), daemon=True).start()
    time.sleep(2)

device = initiate_device("")
it = EventsIterator.from_device(device=device)
h, w = it.get_size()
print(f"[DVS] geöffnet: {w}x{h}", flush=True)

fg = None
if not args.no_frames:
    fg = PeriodicFrameGenerationAlgorithm(sensor_width=w, sensor_height=h, fps=25,
                                          palette=ColorPalette.Dark)
    fg.set_output_callback(lambda ts, f: stats.__setitem__("frames", stats["frames"] + 1))

threading.Thread(target=monitor, daemon=True).start()
t_start = time.monotonic()
for evs in it:
    t = time.perf_counter()
    stats["ev"] += len(evs)
    stats["batches"] += 1
    if fg is not None:
        fg.process_events(evs)
    stats["proc"] += time.perf_counter() - t
    stats["last_batch"] = time.monotonic()
    if time.monotonic() - t_start > args.seconds:
        break
else:
    print("ERGEBNIS: Iterator ist von selbst zu Ende gegangen (Kamera weg?)", flush=True)

stop.set()
print(f"ERGEBNIS: {args.seconds:.0f} s ohne Hänger durchgelaufen.", flush=True)
