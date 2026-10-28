"""Helpers shared by the xeuss20 counter controllers."""


def emit_pending(slave):
    """Send the controller rows that the reading loop has not sent yet to the scan.

    Called from stop_device(): on an abort the reading loop ends before the point is complete; a controller
    that still stores a (partial) value for that point (_data) gets it into the scan like a normal point, so
    an interrupted scan has the same data structure as a complete one. Returns the number of rows sent."""
    device = slave.device
    rows = len(device._data) - slave._nb_acq_points
    if rows <= 0:
        return 0
    counters = list(slave._counters.keys())
    values = device.get_values(slave._nb_acq_points, *counters)
    data = [cnt.conversion_function(v) for cnt, v in zip(counters, values)]
    slave._nb_acq_points += rows
    slave._emit_new_data(data)
    return rows
