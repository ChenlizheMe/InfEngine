"""
Timed physics stress benchmark for Experiment 4.

Rigid-body cubes are spawned at y=50 m and released under gravity.
Sampling windows are delimited by **elapsed wall-clock time** (not frame
count) so that boundaries align with actual physics events regardless of FPS:

    Warm-up    0 s –  1 s   (not recorded — let spawning settle)
    Free-fall  1 s –  4 s   Cubes in the air, sparse contacts
    Settling   4 s –  9 s   Dense contacts after landing (~3 s)
    Resting    9 s – 14 s   Sleep-eligible bodies

After 14 s the mean FPS for each interval is printed and saved to CSV.

Usage
─────
**Option A — single run (Inspector):**
  Attach to a GameObject, set ``cube_count``, press Play.

**Option B — multi-run from editor console:**
    >>> comp = find("BenchHost").get_component(PhysicsBenchmarkTimeline)
  >>> comp.run_sweep([100, 200, 500, 1000, 2000, 3000, 4000, 5000])
  Each count finishes → auto-destroys → spawns next count.

**Option C — restart with a specific count mid-play:**
  >>> comp.restart(2000)

Physics alignment notes (match these in Unity):
  - Fixed timestep  : 1/50 s  (set in Project Settings)
  - Friction        : 0.5
  - Restitution     : 0.3
  - Solver velocity iterations : 10
  - Solver position iterations : 2
  - Collision detection : Discrete
"""

from Infernux.components import InxComponent
from Infernux.instantiate import Destroy
from Infernux.lib import InxPhysicMaterial, SceneManager, PrimitiveType, Vector3
from Infernux.debug import debug
from Infernux.engine.path_utils import resolved_path
import csv
import os
import random


# ── Time boundaries (seconds from spawn) ───────────────────────
WARMUP_END    =  1.0   # seconds — ignore the first second (spawn hiccup)
FREEFALL_END  =  4.0   # free-fall ends   (cubes land ≈ 3.2 s)
SETTLING_END  =  9.0   # settling ends    (user reports stable ≈ 8 s)
RESTING_END   = 14.0   # resting recording ends

# ── Physics material constants ──────────────────────────────────
FRICTION    = 0.5
RESTITUTION = 0.3

# ── Spawn layout ────────────────────────────────────────────────
SPAWN_HEIGHT    = 50.0
SPAWN_RANGE     = 25.0      # cubes in [-25, 25] XZ
GROUND_HALFSIZE = 50.0


class PhysicsBenchmarkTimeline(InxComponent):
    """Attach to any GameObject.  Set *cube_count* in the Inspector."""

    cube_count: int = 1000

    # ────────────────────────────────────────────────────────────
    def start(self):
        self._scene = None
        self._floor = None
        self._cubes = []          # references to spawned GameObjects
        self._sweep_queue = []    # remaining counts for run_sweep()
        self._running = False
        self._done    = True
        self._physic_material = InxPhysicMaterial()
        self._physic_material.friction = FRICTION
        self._physic_material.bounciness = RESTITUTION

        self._init_scene()
        self._begin_run(self.cube_count)

    # ────────────────────────────────────────────────────────────
    #  Public API — call from editor console
    # ────────────────────────────────────────────────────────────
    def restart(self, num: int):
        """Destroy all cubes and start a new experiment with *num* bodies."""
        self._destroy_cubes()
        self._begin_run(num)

    def run_sweep(self, counts):
        """Queue a list of body counts.  Each finishes → auto-starts next.

        Example:
            comp.run_sweep([100, 500, 1000, 3000, 5000])
        """
        if not counts:
            return
        self._sweep_queue = list(counts)
        first = self._sweep_queue.pop(0)
        self.restart(first)

    # ────────────────────────────────────────────────────────────
    def update(self, dt: float):
        if not self._running:
            return

        self._elapsed += dt
        t = self._elapsed

        # ── warm-up period: skip recording ──
        if t < WARMUP_END:
            return

        # ── record instantaneous FPS ──
        fps = (1.0 / dt) if dt > 1e-6 else 0.0

        if t < FREEFALL_END:
            self._samples["freefall"].append(fps)
        elif t < SETTLING_END:
            self._samples["settling"].append(fps)
        elif t < RESTING_END:
            self._samples["resting"].append(fps)

        # ── all intervals done ──
        if t >= RESTING_END:
            self._report()
            self._running = False
            self._done    = True
            # auto-advance sweep queue
            if self._sweep_queue:
                next_count = self._sweep_queue.pop(0)
                debug.log(f"[PhysBench] Sweep: advancing to {next_count} bodies …")
                self.restart(next_count)

    # ────────────────────────────────────────────────────────────
    #  Scene setup / teardown
    # ────────────────────────────────────────────────────────────
    def _init_scene(self):
        sm = SceneManager.instance()
        self._scene = sm.get_active_scene()
        if self._scene is None:
            debug.log_error("[PhysBench] No active scene!")
            return

        # static ground plane (created once, kept across runs)
        self._floor = self._scene.create_primitive(
            PrimitiveType.Plane, "BenchFloor"
        )
        self._floor.transform.local_scale = Vector3(
            GROUND_HALFSIZE, 1, GROUND_HALFSIZE
        )
        self._floor.transform.position = Vector3(0, 0, 0)
        col = self._floor.add_component("BoxCollider")
        col.physic_material = self._physic_material

    def _begin_run(self, num: int):
        """Reset counters, spawn *num* cubes, start recording."""
        self.cube_count = num
        self._destroy_cubes()
        self._spawn_cubes(num)

        self._elapsed = 0.0        # wall-clock seconds since spawn
        self._running = True
        self._done    = False

        self._samples = {
            "freefall": [],
            "settling": [],
            "resting":  [],
        }

        debug.log(
            f"[PhysBench] Spawned {num} cubes.  "
            f"Intervals: warm-up < {WARMUP_END}s, "
            f"free-fall < {FREEFALL_END}s, "
            f"settling < {SETTLING_END}s, "
            f"resting < {RESTING_END}s"
        )

    def _spawn_cubes(self, num: int):
        for i in range(num):
            cube = self._scene.create_primitive(
                PrimitiveType.Cube, f"PhysCube_{i}"
            )
            x = random.uniform(-SPAWN_RANGE, SPAWN_RANGE)
            z = random.uniform(-SPAWN_RANGE, SPAWN_RANGE)
            y = SPAWN_HEIGHT + random.uniform(0, 5.0)
            cube.transform.position = Vector3(x, y, z)

            col = cube.add_component("BoxCollider")
            col.physic_material = self._physic_material

            rb = cube.add_component("Rigidbody")
            rb.mass = 1.0
            rb.use_gravity = True

            self._cubes.append(cube)

    def _destroy_cubes(self):
        """Destroy all previously spawned cubes."""
        for cube in self._cubes:
            try:
                Destroy(cube)
            except Exception:
                pass  # already destroyed
        self._cubes.clear()

    # ────────────────────────────────────────────────────────────
    #  Reporting
    # ────────────────────────────────────────────────────────────
    @staticmethod
    def _mean(values):
        return sum(values) / len(values) if values else 0.0

    def _report(self):
        mean_ff = self._mean(self._samples["freefall"])
        mean_st = self._mean(self._samples["settling"])
        mean_rt = self._mean(self._samples["resting"])

        n_ff = len(self._samples["freefall"])
        n_st = len(self._samples["settling"])
        n_rt = len(self._samples["resting"])

        header = (
            f"\n{'=' * 56}\n"
            f"  Physics Benchmark — {self.cube_count} bodies\n"
            f"{'=' * 56}\n"
            f"  {'Interval':<14} {'Frames':>8} {'Mean FPS':>10}\n"
            f"  {'-' * 40}\n"
            f"  {'Free-fall':<14} {n_ff:>8} {mean_ff:>10.1f}\n"
            f"  {'Settling':<14} {n_st:>8} {mean_st:>10.1f}\n"
            f"  {'Resting':<14} {n_rt:>8} {mean_rt:>10.1f}\n"
            f"{'=' * 56}"
        )
        debug.log(header)

        # ── append to CSV ──
        self._write_csv(mean_ff, mean_st, mean_rt)

    def _write_csv(self, mean_ff, mean_st, mean_rt):
        csv_path = os.path.join(
            os.path.dirname(resolved_path(__file__)),
            "..", "..", "..",     # up to project root
            "physics_bench_results.csv",
        )
        csv_path = resolved_path(csv_path)

        file_exists = os.path.isfile(csv_path)
        try:
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow([
                        "bodies",
                        "freefall_fps",
                        "settling_fps",
                        "resting_fps",
                    ])
                writer.writerow([
                    self.cube_count,
                    f"{mean_ff:.1f}",
                    f"{mean_st:.1f}",
                    f"{mean_rt:.1f}",
                ])
            debug.log(f"[PhysBench] Results appended to {csv_path}")
        except OSError as e:
            debug.log_error(f"[PhysBench] Could not write CSV: {e}")


PhysicsBenchmarkThreePhase = PhysicsBenchmarkTimeline
