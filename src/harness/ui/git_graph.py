"""Lane layout for the commit graph: which column each commit sits in and which lines join
one row to the next. Pure: no git, no I/O."""

Row = tuple[str, list[str]]  # (sha, parent shas), newest first in topo order
Placed = tuple[int, list[tuple[int, int]]]  # (lane, edges)


def layout(rows: list[Row]) -> tuple[list[Placed], int]:
    """(lane and edges for each row, in order; the number of lanes used)."""
    lanes: list[str | None] = []
    lane_of: list[int] = []
    through_of: list[set[int]] = []
    targets_of: list[list[int]] = []
    after_of: list[list[str | None]] = []
    width = 0

    for sha, parents in rows:
        if sha in lanes:
            lane = lanes.index(sha)
        elif None in lanes:
            lane = lanes.index(None)
        else:
            lanes.append(None)
            lane = len(lanes) - 1
        for i, v in enumerate(lanes):
            if v == sha and i != lane:
                lanes[i] = None
        through = {i for i, v in enumerate(lanes) if v is not None and i != lane}
        lanes[lane] = parents[0] if parents else None
        targets = [lane] if parents else []
        for p in parents[1:]:
            if p in lanes:
                j = lanes.index(p)
            else:
                j = next((i for i, v in enumerate(lanes) if v is None and i != lane), -1)
                if j == -1:
                    lanes.append(None)
                    j = len(lanes) - 1
                lanes[j] = p
            targets.append(j)
        lane_of.append(lane)
        through_of.append(through)
        targets_of.append(targets)
        after_of.append(list(lanes))
        width = max(width, len(lanes))

    result: list[Placed] = []
    for r, (_sha, _parents) in enumerate(rows):
        edges: set[tuple[int, int]] = set()
        for i, v in enumerate(after_of[r]):
            if v is None:
                continue
            if r + 1 < len(rows) and v == rows[r + 1][0]:
                dst = lane_of[r + 1]
            else:
                dst = i
            if i in through_of[r]:
                edges.add((i, dst))
            if i in targets_of[r]:
                edges.add((lane_of[r], dst))
        result.append((lane_of[r], sorted(edges)))
    return result, width
