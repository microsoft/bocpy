"""Boids flocking simulation using behavior-oriented concurrency."""

from ast import Set
from collections import deque
import colorsys
import math
from typing import Mapping, NamedTuple

from bocpy import Cown, Matrix, receive, send, start, wait, when


class BoundingBox(NamedTuple("BoundingBox", [("left", int), ("top", int), ("right", int), ("bottom", int)])):
    """A rectangular region."""

    def is_outside(self, x: float, y: float) -> bool:
        """Determine whether the point is outside the box.

        :param x: The x coordinate.
        :param y: The y coordinate.
        :return: ``True`` if the point is outside the rectangle.
        """
        return x < self.left or x > self.right or y < self.top or y > self.bottom


def init_boids(num_boids: int, width: int, height: int) -> tuple[Matrix, Matrix]:
    """Initialize boids with random positions and velocities.

    :param num_boids: The number of boids to initialize.
    :param width: The initial width of the space.
    :param height: The initial height of the space.
    :return: A ``(positions, velocities)`` tuple of *N* x 2 matrices.
    """
    positions = Matrix.uniform(size=(num_boids, 2)) * [width, height]
    velocities = Matrix.uniform(-5, 5, (num_boids, 2))
    return positions, velocities


def keep_within_bounds(pos: Matrix,
                       width: int, height: int,
                       margin=200, turn_factor=1) -> Matrix:
    """Compute a turn-away velocity adjustment to keep a boid in bounds.

    :param pos: The boid's current position (1 x 2).
    :param width: The width of the simulation area.
    :param height: The height of the simulation area.
    :param margin: Distance from each edge at which turning begins.
    :param turn_factor: Magnitude of the corrective velocity.
    :return: A 1 x 2 velocity delta.
    """
    dv = Matrix(1, 2)
    if pos.x < margin:
        dv.x += turn_factor

    if pos.x > width - margin:
        dv.x -= turn_factor

    if pos.y < margin:
        dv.y += turn_factor

    if pos.y > height - margin:
        dv.y -= turn_factor

    return dv


def fly_toward_center(neighbors: Matrix, boid: Matrix,
                      centering_factor=0.005) -> Matrix:
    """Compute a velocity adjustment that steers toward the flock center.

    :param neighbors: An *N* x 2 matrix of neighbor positions.
    :param boid: The boid's current position (1 x 2).
    :param centering_factor: Strength of the centering force.
    :return: A 1 x 2 velocity delta.
    """
    return (neighbors.mean(0) - boid) * centering_factor


def avoid_others(neighbors: Matrix, pos: Matrix,
                 min_distance=20, avoid_factor=0.05) -> Matrix:
    """Compute a velocity adjustment that steers away from nearby boids.

    :param neighbors: An *N* x 2 matrix of neighbor positions.
    :param pos: The boid's current position (1 x 2).
    :param min_distance: Radius within which neighbors are considered too close.
    :param avoid_factor: Strength of the avoidance force.
    :return: A 1 x 2 velocity delta.
    """
    left = pos.x - min_distance
    top = pos.y - min_distance
    right = left + 2 * min_distance
    bottom = top + 2 * min_distance
    bounds = BoundingBox(left, top, right, bottom)

    move = Matrix.vector([0, 0])
    for npos in neighbors:
        if bounds.is_outside(npos.x, npos.y):
            continue

        move += pos - npos

    return move * avoid_factor


def match_velocity(velocities: Matrix, boid: Matrix, matching_factor=0.05) -> Matrix:
    """Compute a velocity adjustment that aligns with the flock's average heading.

    :param velocities: An *N* x 2 matrix of neighbor velocities.
    :param boid: The boid's current velocity (1 x 2).
    :param matching_factor: Strength of the alignment force.
    :return: A 1 x 2 velocity delta.
    """
    return (velocities.mean(0) - boid) * matching_factor


def limit_speed(velocity: Matrix, speed_limit=15):
    """Clamp a boid's velocity to the speed limit in place.

    :param velocity: A 1 x 2 velocity vector (modified in place).
    :param speed_limit: The maximum speed for a boid
    """
    speed = velocity.magnitude()
    if speed > speed_limit:
        velocity /= speed
        velocity *= speed_limit


def int_coord(v: float, spacing: float) -> int:
    """Map a continuous coordinate to a discrete grid index.

    :param v: The coordinate value.
    :param spacing: The width of each grid cell.
    :return: The grid index.
    """
    return int(math.floor(v / spacing))


def hash_coords(xi: int, yi: int, num_cells: int) -> int:
    """Hash a 2-D grid cell coordinate into a flat cell index.

    :param xi: The grid row index.
    :param yi: The grid column index.
    :param num_cells: The total number of hash buckets.
    :return: An index in ``[0, num_cells)``.
    """
    cache = {}

    def f(xi: int, yi: int):
        key = (xi, yi)
        if key not in cache:
            h = (xi * 92837111) ^ (yi * 689287499)
            cache[key] = abs(h) % num_cells

        return cache[key]

    return f(xi, yi)


class Cell(NamedTuple("Cell", [("row", int), ("column", int)])):
    """A grid cell identified by row and column."""


class CellData(NamedTuple("CellData", [("cell", Cell), ("boids", tuple[int]),
                                       ("positions", Cown[Matrix]), ("velocities", Cown[Matrix])])):
    """Per-cell snapshot of boid indices, positions, and velocities."""

    def update(self, cell_data: Mapping[Cell, "CellData"], width: int, height: int):
        """Schedule a behavior to update the boids in this cell.

        :param cell_data: The full grid mapping, used to locate neighbor cells.
        :param width: The simulation area width.
        :param height: The simulation area height.
        """
        row, column = self.cell
        boids = self.boids
        positions = [self.positions]
        velocities = [self.velocities]

        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue

                nkey = Cell(row + dr, column + dc)
                if nkey in cell_data:
                    ncell = cell_data[nkey]
                    positions.append(ncell.positions)
                    velocities.append(ncell.velocities)

        num_boids = len(boids)
        if num_boids == 1:
            @when(self.positions, self.velocities)
            def _(positions: Cown[Matrix], velocities: Cown[Matrix]):
                pos = positions.value
                vel = velocities.value
                limit_speed(vel)
                vel += keep_within_bounds(pos, width, height)
                pos += vel
                send("update", (row, column, pos.copy(), vel.copy()))

            return

        @when(positions, velocities)
        def _(positions: list[Cown[Matrix]], velocities: list[Cown[Matrix]]):
            batch_positions = Matrix.concat([c.value for c in positions])
            batch_velocities = Matrix.concat([c.value for c in velocities])

            pcell = positions[0].value
            vcell = velocities[0].value

            for i in range(num_boids):
                pos = batch_positions[i]
                vel = batch_velocities[i]
                vel += fly_toward_center(batch_positions, pos)
                vel += avoid_others(batch_positions, pos)
                vel += match_velocity(batch_velocities, vel)
                limit_speed(vel)
                vel += keep_within_bounds(pos, width, height)

                vcell[i] = batch_velocities[i] = vel
                pcell[i] = batch_positions[i] = pos + vel

            pos_update = batch_positions[:num_boids]
            vel_update = batch_velocities[:num_boids]
            send("update", (row, column, pos_update, vel_update))


class Simulation:
    """Spatial-hashing boids simulation driven by BOC behaviors."""

    def __init__(self, num_boids: int, width: int, height: int, spacing=50):
        """Create a simulation with the given number of boids.

        :param num_boids: The number of boids.
        :param width: The initial width of the simulation area.
        :param height: The initial height of the simulation area.
        :param spacing: The size of a grid cell (for spatial hasing)
        """
        self.spacing = spacing
        self.num_boids = num_boids
        self.positions, self.velocities = init_boids(num_boids, width, height)
        self.num_cells = 2 * num_boids
        self.cell_start = [0 for _ in range(self.num_cells + 1)]
        self.cell_entries = [0 for _ in range(self.num_cells)]
        self.hash_values = [0 for _ in range(self.num_boids)]
        self.grid_cells: Set[Cell] = set()
        self.cell_data: Mapping[Cell, CellData] = {}
        self.num_behaviors = 0

    def spatial_hashing(self, positions: Matrix):
        """Bin every boid into a hash-grid cell.

        :param positions: An *N* x 2 matrix of boid positions.
        """
        # clear cell start
        for i in range(self.num_cells + 1):
            self.cell_start[i] = 0

        self.grid_cells.clear()

        # first we count how many entries are in each cell
        for i, pos in enumerate(positions):
            r = int_coord(pos.y, self.spacing)
            c = int_coord(pos.x, self.spacing)
            self.grid_cells.add(Cell(r, c))

            h = hash_coords(r, c, self.num_cells)
            self.hash_values[i] = h
            self.cell_start[h] += 1

        # perform the cumulative sum
        start = 0
        for i in range(self.num_cells):
            start += self.cell_start[i]
            self.cell_start[i] = start

        self.cell_start[-1] = start

        # populate the cell entries
        for i in range(self.num_boids):
            h = self.hash_values[i]
            # the effect is that we fill from the back. Once all
            # nodes have been placed, the start will be at the
            # beginning of the cell entries.
            self.cell_start[h] -= 1
            self.cell_entries[self.cell_start[h]] = i

    def build_cell_data(self, positions: Matrix, velocities: Matrix, row: int, column: int) -> CellData:
        """Build a :class:`CellData` snapshot for a single grid cell.

        :param positions: The full *N* x 2 positions matrix.
        :param velocities: The full *N* x 2 velocities matrix.
        :param row: The grid row.
        :param column: The grid column.
        :return: A :class:`CellData` containing the boids that fall within this cell.
        """
        left = column * self.spacing
        top = row * self.spacing
        right = left + self.spacing
        bottom = top + self.spacing
        box = BoundingBox(left, top, right, bottom)

        h = hash_coords(row, column, self.num_cells)
        start = self.cell_start[h]
        end = self.cell_start[h + 1]
        boids = []
        for i in range(start, end):
            b = self.cell_entries[i]
            pos = positions[b]
            if box.is_outside(pos.x, pos.y):
                continue

            boids.append(b)

        assert len(boids) > 0, "Invalid grid cell"

        positions = positions.select(boids)
        velocities = velocities.select(boids)
        return CellData(Cell(row, column), tuple(boids), Cown(positions), Cown(velocities))

    def step(self, width: int, height: int):
        """Run one simulation step: hash, schedule behaviors, and collect results.

        :param width: The current simulation area width.
        :param height: The current simulation area height.
        """
        self.spatial_hashing(self.positions)

        for cell in self.grid_cells:
            self.cell_data[cell] = self.build_cell_data(self.positions, self.velocities, cell.row, cell.column)

        self.num_behaviors = 0
        for value in self.cell_data.values():
            value.update(self.cell_data, width, height)
            self.num_behaviors += 1

        for _ in range(self.num_behaviors):
            _, (row, column, positions, velocities) = receive("update")
            boids = self.cell_data[Cell(row, column)].boids
            for b, pos, vel in zip(boids, positions, velocities):
                self.positions[b] = pos
                self.velocities[b] = vel

        self.cell_data.clear()


def main():
    """Launch the pyglet window and run the boids simulation."""
    import argparse
    import pyglet

    class Boids(pyglet.window.Window):
        """Pyglet window that renders a boids simulation."""

        def __init__(self, width: int, height: int, num_boids: int,
                     show_overlay: bool = True):
            """Initialize the window and create boids.

            :param width: Window width in pixels.
            :param height: Window height in pixels.
            :param num_boids: The number of boids to simulate.
            :param show_overlay: Whether to render the boid count and
                behavior-rate overlay in the bottom-left corner.
            """
            pyglet.window.Window.__init__(self, width, height, "Boids")
            pyglet.gl.glClearColor(1, 1, 1, 1)
            self.batch = pyglet.graphics.Batch()
            self.elapsed = 0
            self.simulation = Simulation(num_boids, width, height)
            self.num_behaviors = 0
            self.samples = deque()
            self.show_overlay = show_overlay

            if show_overlay:
                self.num_boids_label = pyglet.text.Label(
                    f"#boids: {num_boids}",
                    font_size=24, x=5, y=5,
                    color=(100, 100, 100, 255))

                self.behaviors_label = pyglet.text.Label(
                    "behavior/s: ",
                    font_size=24, x=5, y=50,
                    color=(100, 100, 100, 255))
            else:
                self.num_boids_label = None
                self.behaviors_label = None

            self.triangles: pyglet.shapes.Triangle = []
            for _ in range(num_boids):
                tri = pyglet.shapes.Triangle(0, 0, -20, +7, -20, -7,
                                             color=(55, 255, 255, 255),
                                             batch=self.batch)
                tri.anchor_position = 0, 0
                self.triangles.append(tri)

        def on_draw(self):
            """Clear the window and draw all boid triangles."""
            self.clear()
            self.batch.draw()
            if self.show_overlay:
                self.num_boids_label.draw()
                self.behaviors_label.draw()

        def on_close(self):
            wait()
            self.close()

        def update(self, delta_time: float):
            """Advance the simulation by one frame.

            :param delta_time: Seconds elapsed since the last frame.
            """
            self.elapsed += delta_time
            self.simulation.step(self.width, self.height)
            self.num_behaviors += self.simulation.num_behaviors

            if self.elapsed > 1:
                self.samples.append(self.num_behaviors / self.elapsed)
                self.num_behaviors = 0
                self.elapsed = 0
                if len(self.samples) > 10:
                    self.samples.popleft()

            if len(self.samples) > 3 and self.behaviors_label is not None:
                behavior_rate = sum(self.samples) / len(self.samples)
                self.behaviors_label.text = f"behavior/s: {behavior_rate:.0f}"

            positions = self.simulation.positions
            velocities = self.simulation.velocities
            for b, t in enumerate(self.triangles):
                pos = positions[b]
                vel = velocities[b]
                angle = math.atan2(vel.y, vel.x)
                r, g, b = colorsys.hsv_to_rgb(((angle + math.pi) / (2 * math.pi)), 1, 1)
                r = int(r * 255)
                g = int(g * 255)
                b = int(b * 255)
                t.color = (r, g, b, 255)
                t.position = pos.x, pos.y
                t.rotation = -angle * 180 / math.pi

    parser = argparse.ArgumentParser("Boids")
    parser.add_argument("--boids", "-b", type=int, default=300)
    parser.add_argument("--width", type=int, default=1200)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--mode", choices=("window", "video"),
                        default="window",
                        help="window: interactive (default); "
                             "video: render and pipe frames to ffmpeg.")
    parser.add_argument("--duration", type=float, default=30.0,
                        help="Seconds to simulate in video mode.")
    parser.add_argument("--output", "-o", default="boids.mp4",
                        help="Output path for video mode.")
    parser.add_argument("--fps", type=int, default=30,
                        help="Simulation/render rate. In video mode this is "
                             "the encoded frame rate; in window mode this is "
                             "the scheduled tick rate. The simulation "
                             "integrates one step per tick, so this value "
                             "controls on-screen speed in both modes.")
    parser.add_argument("--workers", type=int, default=None,
                        help="Number of BOC worker sub-interpreters. "
                             "Defaults to bocpy's default (CPU count - 1).")
    args = parser.parse_args()

    # Validate at the boundary; downstream code (Matrix sizing, hash modulo,
    # 1.0/fps) assumes positive values and would crash or silently misbehave.
    if args.boids <= 0:
        parser.error("--boids must be positive")
    if args.width <= 0 or args.height <= 0:
        parser.error("--width and --height must be positive")
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if args.workers is not None and args.workers <= 0:
        parser.error("--workers must be positive")

    # Start the BOC runtime explicitly so --workers takes effect for every
    # mode.
    start(worker_count=args.workers)

    if args.mode == "video":
        import subprocess

        # Create the window first so we can query the actual framebuffer
        # dimensions (which may differ from logical size on HiDPI displays).
        # The overlay (boid count / behavior rate) is suppressed in video
        # mode so the rendered output stays clean.
        boids = Boids(args.width, args.height, args.boids,
                      show_overlay=False)

        # Allow graceful close: override on_close to set a flag and return
        # True so pyglet does not destroy the window mid-frame. The loop
        # below honors the flag; the finally block tears the window down.
        # Use a bocpy-prefixed attribute name to avoid colliding with any
        # underscore-prefixed pyglet internals.
        boids.bocpy_video_closing = False

        def _on_close():
            boids.bocpy_video_closing = True
            return True

        boids.on_close = _on_close

        # Determine the real framebuffer size (HiDPI-correct).
        boids.switch_to()
        boids.dispatch_events()
        boids.clear()
        boids.batch.draw()
        first_buf = pyglet.image.get_buffer_manager().get_color_buffer()
        fb_width = first_buf.width
        fb_height = first_buf.height
        if (fb_width, fb_height) != (args.width, args.height):
            print(f"note: framebuffer is {fb_width}x{fb_height} "
                  f"(window requested {args.width}x{args.height}); "
                  f"encoding at framebuffer resolution.")

        # Validate frame count BEFORE spawning ffmpeg so we don't leak the
        # subprocess if the duration/fps combination produces no frames.
        num_frames = int(args.duration * args.fps)
        if num_frames == 0:
            print(f"error: --duration {args.duration} is too short for "
                  f"--fps {args.fps} (no frames would be written).")
            boids.close()
            wait()
            return

        try:
            ff = subprocess.Popen(
                [
                    "ffmpeg", "-y", "-loglevel", "warning",
                    "-f", "rawvideo", "-pix_fmt", "rgba",
                    "-s", f"{fb_width}x{fb_height}",
                    "-r", str(args.fps),
                    "-i", "-",
                    "-vf", "vflip",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    args.output,
                ],
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            print("error: ffmpeg not found on PATH; install ffmpeg or use "
                  "--mode headless.")
            boids.close()
            wait()
            return
        except OSError as exc:
            # Other startup failures (read-only output dir, ENOMEM, etc.)
            # also need cleanup to avoid leaking the window/runtime.
            print(f"error: failed to start ffmpeg: {exc}")
            boids.close()
            wait()
            return

        dt = 1.0 / args.fps
        frames_written = 0
        ff_stderr: bytes | None = b""
        try:
            for _ in range(num_frames):
                if boids.bocpy_video_closing:
                    break

                boids.switch_to()
                boids.dispatch_events()
                if boids.bocpy_video_closing:
                    break

                boids.update(dt)
                boids.clear()
                boids.batch.draw()
                if boids.show_overlay:
                    boids.num_boids_label.draw()
                    boids.behaviors_label.draw()

                buf = pyglet.image.get_buffer_manager().get_color_buffer()
                # Defensive: framebuffer size must remain stable for the
                # encoder. If it changes (window manager fiddling, monitor
                # move) we abort rather than emit garbled frames.
                if (buf.width, buf.height) != (fb_width, fb_height):
                    print(f"error: framebuffer size changed mid-record "
                          f"({fb_width}x{fb_height} -> "
                          f"{buf.width}x{buf.height}); stopping.")
                    break

                data = buf.get_image_data().get_data("RGBA", buf.width * 4)
                try:
                    ff.stdin.write(data)
                except BrokenPipeError:
                    print("error: ffmpeg pipe closed unexpectedly.")
                    break
                frames_written += 1
                boids.flip()
        except KeyboardInterrupt:
            print("(interrupted)")
        finally:
            try:
                try:
                    if ff.stdin is not None:
                        ff.stdin.close()
                except OSError:
                    pass
                try:
                    _, ff_stderr = ff.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    print("warning: ffmpeg did not exit within 30s; killing.")
                    ff.kill()
                    try:
                        _, ff_stderr = ff.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                # Always release the pyglet window and BOC runtime, even if
                # ffmpeg cleanup raised something unexpected.
                try:
                    boids.close()
                finally:
                    wait()

        if ff.returncode != 0:
            if ff.returncode is None:
                # We tried to kill ffmpeg but it never reaped within 5s after
                # SIGKILL. The output file (if any) is almost certainly
                # truncated and missing the libx264 moov atom.
                print("error: ffmpeg was killed and did not exit; "
                      "output file is likely truncated.")
            else:
                print(f"error: ffmpeg exited with status {ff.returncode}.")
            if ff_stderr:
                print(ff_stderr.decode("utf-8", errors="replace"), end="")
            return

        print(f"Wrote {args.output} ({frames_written} frames)"
              f"{' (interrupted)' if boids.bocpy_video_closing else ''}")
        return

    boids = Boids(args.width, args.height, args.boids)
    pyglet.clock.schedule_interval(boids.update, 1 / args.fps)
    pyglet.app.run()


if __name__ == "__main__":
    main()
