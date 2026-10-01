-- R7: synthetic metric series for the z-score detector. 24h retention via Runner.prune().
-- The primary key doubles as the (service, metric, ts) index and makes generator re-runs no-ops.
CREATE TABLE IF NOT EXISTS metric_points (
    service text NOT NULL,
    metric  text NOT NULL,
    ts      timestamptz NOT NULL,
    value   double precision NOT NULL,
    PRIMARY KEY (service, metric, ts)
);
