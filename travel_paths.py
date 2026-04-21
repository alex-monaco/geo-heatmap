#!/usr/bin/env python3

from argparse import ArgumentParser, RawTextHelpFormatter
import bisect
import collections
from datetime import date, datetime
import json
import math
import os
import webbrowser

import folium
from utils import isTextBasedBrowser, dateInRange

ACTIVITY_CONFIG = {
    "walking":              {"color": "#4CAF50", "name": "Walking"},
    "running":              {"color": "#8BC34A", "name": "Running"},
    "cycling":              {"color": "#2196F3", "name": "Cycling"},
    "in passenger vehicle": {"color": "#FF9800", "name": "Driving"},
    "motorcycling":         {"color": "#FFC107", "name": "Motorcycling"},
    "in bus":               {"color": "#FF5722", "name": "Bus"},
    "in train":             {"color": "#9C27B0", "name": "Train"},
    "in tram":              {"color": "#CE93D8", "name": "Tram"},
    "in subway":            {"color": "#795548", "name": "Subway"},
    "flying":               {"color": "#F44336", "name": "Flying"},
    "in ferry":             {"color": "#00BCD4", "name": "Ferry"},
    "sailing":              {"color": "#006064", "name": "Sailing"},
    "skiing":               {"color": "#B3E5FC", "name": "Skiing"},
    "unknown":              {"color": "#9E9E9E", "name": "Unknown"},
}


def parse_ts(ts_str):
    """Parse an ISO timestamp to a UTC unix float."""
    ts_str = ts_str.replace("Z", "+00:00")
    return datetime.fromisoformat(ts_str).timestamp()


def parse_geo(geo_str):
    _, coords = geo_str.split(":", 1)
    lat, lon = coords.split(",")
    return (round(float(lat), 6), round(float(lon), 6))


def great_circle_arc(start, end, n=40):
    lat1, lon1 = math.radians(start[0]), math.radians(start[1])
    lat2, lon2 = math.radians(end[0]), math.radians(end[1])
    d = 2 * math.asin(math.sqrt(
        math.sin((lat2 - lat1) / 2) ** 2 +
        math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    ))
    if d < 1e-9:
        return [start, end]
    points = []
    for i in range(n + 1):
        f = i / n
        a = math.sin((1 - f) * d) / math.sin(d)
        b = math.sin(f * d) / math.sin(d)
        x = a * math.cos(lat1) * math.cos(lon1) + b * math.cos(lat2) * math.cos(lon2)
        y = a * math.cos(lat1) * math.sin(lon1) + b * math.cos(lat2) * math.sin(lon2)
        z = a * math.sin(lat1) + b * math.sin(lat2)
        lat = math.degrees(math.atan2(z, math.sqrt(x ** 2 + y ** 2)))
        lon = math.degrees(math.atan2(y, x))
        points.append((round(lat, 6), round(lon, 6)))
    return points


def build_activity_index(data):
    """
    Returns a sorted list of (start_ts, end_ts, activity_type) for fast lookup.
    Also returns start_ts list for bisect.
    """
    entries = []
    for r in data:
        if "activity" not in r:
            continue
        act = r["activity"]
        act_type = act.get("topCandidate", {}).get("type", "unknown").lower()
        try:
            start = parse_ts(r["startTime"])
            end = parse_ts(r["endTime"])
        except (KeyError, ValueError):
            continue
        entries.append((start, end, act_type))
    entries.sort()
    starts = [e[0] for e in entries]
    return entries, starts


def lookup_activity(ts, entries, starts):
    """Find the activity type that contains timestamp ts, or None."""
    # Find the last activity that started at or before ts
    idx = bisect.bisect_right(starts, ts) - 1
    # Check a small window of candidates (activities can overlap)
    for i in range(max(0, idx), max(0, idx - 5), -1):
        start, end, act_type = entries[i]
        if start <= ts <= end:
            return act_type
    return None


def load_path_segments(path, date_range):
    """
    Extract (points_list, activity_type) segments using timelinePath waypoints
    joined to activity records by timestamp. Falls back to point-A-to-B for
    flying segments which won't have path data.
    """
    with open(path) as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("Only the new Google Timeline JSON format (array) is supported.")

    activity_entries, activity_starts = build_activity_index(data)

    segments = []  # list of (points, activity_type)

    # --- Real paths from timelinePath records ---
    for record in data:
        if "timelinePath" not in record:
            continue
        pts = record["timelinePath"]
        if not pts:
            continue
        ts_str = record.get("startTime", "")
        if ts_str and not dateInRange(ts_str[:10], date_range):
            continue
        try:
            block_start = parse_ts(ts_str)
        except ValueError:
            continue

        # Reconstruct (timestamp, lat, lon) for each waypoint
        waypoints = []
        for pt in pts:
            try:
                offset_min = int(pt["durationMinutesOffsetFromStartTime"])
                ts = block_start + offset_min * 60
                lat, lon = parse_geo(pt["point"])
                waypoints.append((ts, lat, lon))
            except (KeyError, ValueError):
                continue

        if not waypoints:
            continue

        # Assign activity type to each waypoint, then group consecutive same-type into segments
        typed = []
        for ts, lat, lon in waypoints:
            act = lookup_activity(ts, activity_entries, activity_starts) or "unknown"
            typed.append((lat, lon, act))

        # Group into runs of the same activity type
        current_type = typed[0][2]
        current_points = [(typed[0][0], typed[0][1])]
        for lat, lon, act in typed[1:]:
            if act == current_type:
                current_points.append((lat, lon))
            else:
                if len(current_points) >= 2:
                    segments.append((current_points, current_type))
                current_type = act
                current_points = [(lat, lon)]
        if len(current_points) >= 2:
            segments.append((current_points, current_type))

    # --- Flying segments from activity records (no path data, use great circle arc) ---
    for record in data:
        if "activity" not in record:
            continue
        act = record["activity"]
        act_type = act.get("topCandidate", {}).get("type", "unknown").lower()
        if act_type != "flying":
            continue
        ts_str = record.get("startTime", "")
        if ts_str and not dateInRange(ts_str[:10], date_range):
            continue
        start_str = act.get("start", "")
        end_str = act.get("end", "")
        if start_str.startswith("geo:") and end_str.startswith("geo:"):
            arc = great_circle_arc(parse_geo(start_str), parse_geo(end_str))
            segments.append((arc, "flying"))

    return segments


def aggregate_edges(segments, precision=3):
    """
    Snap all path coordinates to a ~110m grid and count how many times each
    directed edge (p1 -> p2) is traveled per activity type. Flying segments
    are kept as full arcs and not aggregated.

    Returns:
        edges: dict {act_type: Counter {(p1, p2): count}}
        flying_arcs: list of point lists for flying segments
    """
    edges = collections.defaultdict(collections.Counter)
    flying_arcs = []

    for points, act_type in segments:
        if act_type == "flying":
            flying_arcs.append(points)
            continue
        rounded = [(round(p[0], precision), round(p[1], precision)) for p in points]
        for i in range(len(rounded) - 1):
            p1, p2 = rounded[i], rounded[i + 1]
            if p1 != p2:
                edges[act_type][(p1, p2)] += 1

    return edges, flying_arcs


def edge_weight(count):
    """Map a travel count to a line width. Single trip = 1px, busy route up to ~8px."""
    return min(8, 1 + math.log2(count) * 1.5)


def generate_map(segments, zoom_start=4, tiles="CartoDB positron"):
    if not segments:
        raise ValueError("No path segments found.")

    all_lats = [p[0] for pts, _ in segments for p in pts]
    all_lons = [p[1] for pts, _ in segments for p in pts]
    center = (sum(all_lats) / len(all_lats), sum(all_lons) / len(all_lons))

    m = folium.Map(
        location=center,
        zoom_start=zoom_start,
        tiles=tiles,
        attr='<a href=https://github.com/luka1199/geo-heatmap>geo-heatmap</a>',
    )

    edges, flying_arcs = aggregate_edges(segments)

    # Draw aggregated edges (all non-flying activity types)
    total_unique = sum(len(e) for e in edges.values())
    raw_count = sum(sum(e.values()) for e in edges.values())
    print(f"  Aggregated {raw_count:,} raw edges → {total_unique:,} unique edges")

    for act_type, edge_counts in sorted(edges.items(), key=lambda x: -sum(x[1].values())):
        cfg = ACTIVITY_CONFIG.get(act_type, {"color": "#9E9E9E", "name": act_type.title()})
        unique = len(edge_counts)
        fg = folium.FeatureGroup(name=f"{cfg['name']} ({unique:,} unique edges)", show=True)

        for (p1, p2), count in edge_counts.items():
            folium.PolyLine(
                [p1, p2],
                color=cfg["color"],
                weight=edge_weight(count),
                opacity=0.75,
                tooltip=f"{cfg['name']} × {count}",
            ).add_to(fg)

        fg.add_to(m)

    # Draw flying arcs separately (great circle arcs, not aggregated)
    if flying_arcs:
        cfg = ACTIVITY_CONFIG["flying"]
        fg = folium.FeatureGroup(name=f"Flying ({len(flying_arcs):,} flights)", show=True)
        for arc in flying_arcs:
            folium.PolyLine(
                arc,
                color=cfg["color"],
                weight=1.5,
                opacity=0.6,
                tooltip="Flying",
            ).add_to(fg)
        fg.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    return m


if __name__ == "__main__":
    parser = ArgumentParser(formatter_class=RawTextHelpFormatter)
    parser.add_argument("files", metavar="file", type=str, nargs="+",
                        help="Google Takeout location history JSON file(s)")
    default_output = os.path.join(
        "data", "output", "travel_paths_{}.html".format(date.today().isoformat())
    )
    parser.add_argument("-o", "--output", dest="output", type=str, default=default_output,
                        help="Path of output HTML file.")
    parser.add_argument("--min-date", dest="min_date", metavar="YYYY-MM-DD", type=str, required=False)
    parser.add_argument("--max-date", dest="max_date", metavar="YYYY-MM-DD", type=str, required=False)
    parser.add_argument("-z", "--zoom-start", dest="zoom_start", type=int, default=4)

    args = parser.parse_args()
    date_range = (args.min_date, args.max_date)

    all_segments = []
    for path in args.files:
        segs = load_path_segments(path, date_range)
        print(f"Loaded {len(segs):,} path segments from {path}")
        all_segments.extend(segs)

    by_type = collections.Counter(act for _, act in all_segments)
    print("\nActivity breakdown:")
    for t, c in by_type.most_common():
        cfg = ACTIVITY_CONFIG.get(t, {"name": t.title()})
        print(f"  {cfg['name']:25s} {c:6,}")
    print(f"  {'Total':25s} {len(all_segments):6,}\n")

    m = generate_map(all_segments, zoom_start=args.zoom_start)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    m.save(args.output)
    print(f"Saved to {args.output}")

    if not isTextBasedBrowser(webbrowser.get()):
        try:
            webbrowser.open("file://" + os.path.realpath(args.output))
        except webbrowser.Error:
            print(f"Open {args.output} manually.")
