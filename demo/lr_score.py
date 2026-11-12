#!/usr/bin/env python3
"""Scorer for the learning-rate demo. Trains a small logistic regression with the given schedule and
prints the validation loss. Deterministic: the same schedule always gives the same number.

Reads {"warmup": 0.1, "peak": 0.3, "decay": "cosine"} on stdin, prints {"ok", "score", "detail"}.
"""
import json
import math
import random
import sys

SCALES = [3.0, 2.0, 1.0, 1.0, 0.5, 0.5, 0.2, 0.1]  # features on very different scales, so the rate matters
TRUE = [1.5, -2.0, 1.0, 0.5, -1.0, 2.0, -3.0, 4.0]
DECAYS = ("cosine", "linear", "constant", "invsqrt")
STEPS, BATCH = 120, 16


def make(seed, n):
    rng = random.Random(seed)
    X = [[rng.gauss(0, s) for s in SCALES] for _ in range(n)]
    y = [1.0 if sum(a * b for a, b in zip(TRUE, x)) + rng.gauss(0, 1.0) > 0 else 0.0 for x in X]
    return X, y


TRAIN, VAL = make(1, 400), make(2, 300)


def prob(w, x):
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, sum(a * b for a, b in zip(w, x))))))


def rate(step, a):
    warm = int(a["warmup"] * STEPS)
    if step < warm:
        return a["peak"] * (step + 1) / warm
    t = (step - warm) / max(1, STEPS - warm - 1)
    return a["peak"] * {"cosine": 0.5 * (1 + math.cos(math.pi * t)), "linear": 1 - t, "constant": 1.0, "invsqrt": 1 / math.sqrt(1 + 9 * t)}[a["decay"]]


def score(a):
    """Returns (ok, loss, detail)."""
    try:
        if not (0 <= a["warmup"] <= 0.9 and 0 < a["peak"] <= 100 and a["decay"] in DECAYS):
            raise ValueError
    except (TypeError, KeyError, ValueError):
        return False, None, "Not a valid schedule"
    (X, y), w, rng = TRAIN, [0.0] * len(SCALES), random.Random(3)
    for step in range(STEPS):
        lr, grad = rate(step, a), [0.0] * len(w)
        for i in (rng.randrange(len(X)) for _ in range(BATCH)):
            err = prob(w, X[i]) - y[i]
            for j, xj in enumerate(X[i]):
                grad[j] += err * xj / BATCH
        w = [wj - lr * gj for wj, gj in zip(w, grad)]
    loss = 0.0
    for x, t in zip(*VAL):
        p = min(1 - 1e-9, max(1e-9, prob(w, x)))
        loss -= (t * math.log(p) + (1 - t) * math.log(1 - p)) / len(VAL[0])
    loss = round(loss, 4)
    return (False, loss, "Diverged") if loss > 1.5 else (True, loss, f"{STEPS} steps, batch {BATCH}")


if __name__ == "__main__":
    try:
        ok, value, detail = score(json.load(sys.stdin))
    except ValueError:
        ok, value, detail = False, None, "Artifact was not valid JSON"
    print(json.dumps({"ok": ok, "score": value, "detail": detail}))
