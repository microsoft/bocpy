"""Boids flocking simulation using behavior-oriented concurrency."""

from ast import Set
from collections import deque
import colorsys
import math
from typing import Mapping, NamedTuple

from bocpy import Cown, Matrix, PinnedCown, pump, start, wait, when


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
    if velocity.magnitude_squared() > speed_limit * speed_limit:
        velocity.normalize(in_place=True)
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
        :return: A ``Cown`` holding ``(pos_slice, vel_slice)`` for the
            frame-end pinned writeback to consume.
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
            def single_cell(positions: Cown[Matrix], velocities: Cown[Matrix],
                            height=height, width=width):
                pos = positions.value
                vel = velocities.value
                limit_speed(vel)
                vel += keep_within_bounds(pos, width, height)
                pos += vel
                return pos.copy(), vel.copy()

            return single_cell

        @when(positions, velocities)
        def multi_cell(positions: list[Cown[Matrix]], velocities: list[Cown[Matrix]],
                       height=height, num_boids=num_boids, width=width):
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

            return batch_positions[:num_boids], batch_velocities[:num_boids]

        return multi_cell


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
        positions, velocities = init_boids(num_boids, width, height)
        self.positions_cown = PinnedCown(positions)
        self.velocities_cown = PinnedCown(velocities)
        self.positions = positions
        self.velocities = velocities
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
        for i in range(self.num_cells + 1):
            self.cell_start[i] = 0

        self.grid_cells.clear()

        for i, pos in enumerate(positions):
            r = int_coord(pos.y, self.spacing)
            c = int_coord(pos.x, self.spacing)
            self.grid_cells.add(Cell(r, c))

            h = hash_coords(r, c, self.num_cells)
            self.hash_values[i] = h
            self.cell_start[h] += 1

        start = 0
        for i in range(self.num_cells):
            start += self.cell_start[i]
            self.cell_start[i] = start

        self.cell_start[-1] = start

        for i in range(self.num_boids):
            h = self.hash_values[i]
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

        positions = positions.take(boids)
        velocities = velocities.take(boids)
        return CellData(Cell(row, column), tuple(boids), Cown(positions), Cown(velocities))

    def step(self, width: int, height: int):
        """Run one simulation step: hash, schedule behaviors, and collect results.

        :param width: The current simulation area width.
        :param height: The current simulation area height.
        """
        self.spatial_hashing(self.positions)

        for cell in self.grid_cells:
            self.cell_data[cell] = self.build_cell_data(self.positions, self.velocities, cell.row, cell.column)

        cells = list(self.cell_data.values())
        boid_indices = [cd.boids for cd in cells]
        results = [cd.update(self.cell_data, width, height) for cd in cells]
        self.num_behaviors = len(cells)

        @when(results, self.positions_cown, self.velocities_cown)
        def _writeback(per_cell, all_pos, all_vel, boid_indices=boid_indices):
            pos_mat = all_pos.value
            vel_mat = all_vel.value
            for boids, result in zip(boid_indices, per_cell):
                pos_slice, vel_slice = result.value
                for b, p, v in zip(boids, pos_slice, vel_slice):
                    pos_mat[b] = p
                    vel_mat[b] = v

        self.cell_data.clear()


def main():
    """Launch the pyglet window and run the boids simulation."""
    import argparse
    import pyglet

    class Boids(pyglet.window.Window):
        """Pyglet window that renders a boids simulation."""

        def __init__(self, width: int, height: int, num_boids: int,
                     show_overlay: bool = True, scale: float = 1.0):
            """Initialize the window and create boids.

            :param width: Window width in pixels.
            :param height: Window height in pixels.
            :param num_boids: The number of boids to simulate.
            :param show_overlay: Whether to render the boid count and
                behavior-rate overlay in the bottom-left corner.
            :param scale: Multiplier applied to the drawn boid triangle
                size. ``1.0`` keeps the original 20x14 pixel triangle.
            """
            pyglet.window.Window.__init__(self, width, height, "Boids")
            pyglet.gl.glClearColor(1, 1, 1, 1)
            self.batch = pyglet.graphics.Batch()
            self.elapsed = 0
            self.simulation = Simulation(num_boids, width, height)
            self.num_behaviors = 0
            self.num_frames = 0
            self.total_behaviors = 0
            self.total_elapsed = 0.0
            self.samples = deque()
            self.fps_samples = deque()
            self.show_overlay = show_overlay
            self.pending_updates = 0

            if show_overlay:
                self.num_boids_label = pyglet.text.Label(
                    f"#boids: {num_boids}",
                    font_size=24, x=5, y=5,
                    color=(100, 100, 100, 255))

                self.behaviors_label = pyglet.text.Label(
                    "behavior/s: ",
                    font_size=24, x=5, y=50,
                    color=(100, 100, 100, 255))

                self.fps_label = pyglet.text.Label(
                    "fps: ",
                    font_size=24, x=5, y=95,
                    color=(100, 100, 100, 255))
            else:
                self.num_boids_label = None
                self.behaviors_label = None
                self.fps_label = None

            self.triangles: pyglet.shapes.Triangle = []
            tip_x = 0.0
            base_x = -20.0 * scale
            base_y = 7.0 * scale
            for _ in range(num_boids):
                tri = pyglet.shapes.Triangle(tip_x, 0,
                                             base_x, +base_y,
                                             base_x, -base_y,
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
                self.fps_label.draw()

        def on_close(self):
            wait()
            self.close()

        def update(self, delta_time: float):
            """Advance the simulation by one frame.

            :param delta_time: Seconds elapsed since the last frame.
            """
            self.elapsed += delta_time
            self.total_elapsed += delta_time
            result = pump()
            self.pending_updates -= result.executed
            if self.pending_updates > 0:
                return

            self.pending_updates += 1
            self.simulation.step(self.width, self.height)
            self.num_behaviors += self.simulation.num_behaviors
            self.num_frames += 1
            self.total_behaviors += self.simulation.num_behaviors

            if self.elapsed > 1:
                self.samples.append(self.num_behaviors / self.elapsed)
                self.fps_samples.append(self.num_frames / self.elapsed)
                self.num_behaviors = 0
                self.num_frames = 0
                self.elapsed = 0
                if len(self.samples) > 10:
                    self.samples.popleft()
                if len(self.fps_samples) > 10:
                    self.fps_samples.popleft()

            if len(self.samples) > 3 and self.behaviors_label is not None:
                behavior_rate = sum(self.samples) / len(self.samples)
                self.behaviors_label.text = f"behavior/s: {behavior_rate:.0f}"
                fps = sum(self.fps_samples) / len(self.fps_samples)
                self.fps_label.text = f"fps: {fps:.1f}"

            positions = self.simulation.positions
            velocities = self.simulation.velocities
            angles = velocities.angle()
            for b, t in enumerate(self.triangles):
                pos = positions[b]
                angle = angles[b, 0]
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
    parser.add_argument("--scale", type=float, default=1.0,
                        help="Multiplier applied to the drawn boid "
                             "triangle size (default: 1.0).")
    args = parser.parse_args()

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
    if args.scale <= 0:
        parser.error("--scale must be positive")

    start(worker_count=args.workers)

    if args.mode == "video":
        import subprocess

        boids = Boids(args.width, args.height, args.boids,
                      show_overlay=False, scale=args.scale)

        boids.bocpy_video_closing = False

        def _on_close():
            boids.bocpy_video_closing = True
            return True

        boids.on_close = _on_close

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
                try:
                    boids.close()
                finally:
                    wait()

        if ff.returncode != 0:
            if ff.returncode is None:
                print("error: ffmpeg was killed and did not exit; "
                      "output file is likely truncated.")
            else:
                print(f"error: ffmpeg exited with status {ff.returncode}.")
            if ff_stderr:
                print(ff_stderr.decode("utf-8", errors="replace"), end="")
            return

        if boids.total_elapsed > 0:
            avg_rate = boids.total_behaviors / boids.total_elapsed
            print(f"behavior/s (avg over {boids.total_elapsed:.1f}s of "
                  f"simulated time): {avg_rate:.0f} "
                  f"({boids.total_behaviors} behaviors)")

        print(f"Wrote {args.output} ({frames_written} frames)"
              f"{' (interrupted)' if boids.bocpy_video_closing else ''}")
        return

    boids = Boids(args.width, args.height, args.boids, scale=args.scale)
    pyglet.clock.schedule_interval(boids.update, 1 / args.fps)
    pyglet.app.run()


if __name__ == "__main__":
    main()
