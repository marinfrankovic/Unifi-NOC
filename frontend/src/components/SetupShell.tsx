import { ReactNode, useEffect, useRef } from "react";

type Point = [number, number];
type MeshPath = { points: Point[]; major: boolean; phase: number; duration: number };

function buildMesh(context: CanvasRenderingContext2D, width: number, height: number): MeshPath[] {
  const paths: MeshPath[] = [];
  const groups: Point[] = [[width * .15, height * .27], [width * .85, height * .27], [width * .15, height * .8], [width * .85, height * .8]];
  const radiusScale = Math.min(1, width / 950);
  const stroke = (points: Point[], color: string, lineWidth = 1) => {
    context.beginPath();
    points.forEach(([pointX, pointY], index) => index ? context.lineTo(pointX, pointY) : context.moveTo(pointX, pointY));
    context.strokeStyle = color; context.lineWidth = lineWidth; context.stroke();
  };
  const route = (points: Point[], index: number, major = false) => {
    stroke(points, major ? index % 3 === 0 ? "#6b94c066" : "#599bc475" : "#587fa570", major ? 1.15 : .75);
    paths.push({ points, major, phase: index * .173, duration: major ? 16 + index % 4 : 7 + index % 5 });
  };
  context.fillStyle = "#0b1220"; context.fillRect(0, 0, width, height);
  context.fillStyle = "#304a6880";
  for (let pointX = 28; pointX < width; pointX += 42) for (let pointY = 28; pointY < height; pointY += 42) context.fillRect(pointX, pointY, 1, 1);
  [[0, 1], [0, 2], [1, 3], [2, 3], [0, 3], [1, 2]].forEach(([source, target], index) => {
    const first = groups[source], last = groups[target];
    let controlA: Point, controlB: Point;
    if (index < 4) {
      const horizontal = source === 0 && target === 1 || source === 2 && target === 3;
      const offset = source < 2 && target < 2 ? -105 : horizontal ? 85 : 0;
      controlA = horizontal ? [width * .37, first[1] + offset] : [first[0] + (source === 0 ? -95 : 95) * radiusScale, height * .44];
      controlB = horizontal ? [width * .63, last[1] + offset] : [last[0] + (source === 0 ? -95 : 95) * radiusScale, height * .64];
    } else {
      controlA = [width * (source === 0 ? .25 : .75), height * .55];
      controlB = [width * (source === 0 ? .75 : .25), height * .49];
    }
    const points: Point[] = Array.from({ length: 49 }, (_, step) => {
      const progress = step / 48, rest = 1 - progress;
      return [rest ** 3 * first[0] + 3 * rest ** 2 * progress * controlA[0] + 3 * rest * progress ** 2 * controlB[0] + progress ** 3 * last[0],
        rest ** 3 * first[1] + 3 * rest ** 2 * progress * controlA[1] + 3 * rest * progress ** 2 * controlB[1] + progress ** 3 * last[1]];
    });
    route(points, index, true);
  });
  groups.forEach(([centerX, centerY], group) => {
    const seed = group + 1;
    const points: Point[] = Array.from({ length: 10 }, (_, index) => {
      const angle = index * 2.4 + seed, radius = (30 + (index * 37 + seed * 19) % 100) * radiusScale;
      return [centerX + Math.cos(angle) * radius, centerY + Math.sin(angle) * radius * .72];
    });
    const linked = new Set<string>();
    points.forEach((point, source) => {
      const neighbours = points.map((target, index) => ({ index, distance: Math.hypot(target[0] - point[0], target[1] - point[1]) }))
        .filter(item => item.index !== source).sort((first, second) => first.distance - second.distance).slice(0, 3);
      neighbours.forEach(({ index: target }) => {
        const key = [source, target].sort((first, second) => first - second).join(":");
        if (linked.has(key)) return;
        linked.add(key);
        route([point, points[target]], seed * 11 + source + target);
      });
    });
    points.forEach((point, index) => {
      if (index % 3 === 0) route([[centerX, centerY], point], seed * 17 + index);
      context.fillStyle = index === 2 ? "#88baf0" : "#75a6ce"; context.fillRect(point[0] - 2, point[1] - 2, 4, 4);
    });
    const top: Point[] = [[centerX, centerY - 19], [centerX + 22, centerY - 8], [centerX, centerY + 3], [centerX - 22, centerY - 8]];
    const faces: [Point[], string][] = [
      [[[centerX - 22, centerY - 8], [centerX, centerY + 3], [centerX, centerY + 21], [centerX - 22, centerY + 10]], "#132940"],
      [[[centerX, centerY + 3], [centerX + 22, centerY - 8], [centerX + 22, centerY + 10], [centerX, centerY + 21]], "#18334d"],
      [top, "#203d5b"],
    ];
    faces.forEach(([face, color]) => {
      context.beginPath(); face.forEach(([pointX, pointY], index) => index ? context.lineTo(pointX, pointY) : context.moveTo(pointX, pointY));
      context.closePath(); context.fillStyle = color; context.fill(); context.strokeStyle = "#85b6df"; context.lineWidth = 1; context.stroke();
    });
    stroke([[centerX - 8, centerY + 10], [centerX + 8, centerY + 17]], "#7dd3fc", 2);
  });
  return paths;
}

function MeshBackground() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = canvasRef.current, background = document.createElement("canvas");
    const context = canvas?.getContext("2d"), base = background.getContext("2d");
    if (!canvas || !context || !base) return;
    const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
    let paths: MeshPath[] = [], width = 0, height = 0, frame = 0, previous = 0;
    const draw = (elapsed: number) => {
      context.drawImage(background, 0, 0, width, height);
      paths.forEach(({ points, major, phase, duration }, index) => {
        const progress = (elapsed / duration + phase) % 1;
        const pointAt = (position: number): Point => {
          const offset = Math.max(0, position) * (points.length - 1), segment = Math.min(Math.floor(offset), points.length - 2), fraction = offset - segment;
          const first = points[segment], last = points[segment + 1];
          return [first[0] + (last[0] - first[0]) * fraction, first[1] + (last[1] - first[1]) * fraction];
        };
        context.beginPath();
        for (let step = 0; step <= 8; step++) {
          const [pointX, pointY] = pointAt(progress - .035 + step * .035 / 8);
          if (step) context.lineTo(pointX, pointY); else context.moveTo(pointX, pointY);
        }
        context.strokeStyle = index % 5 === 0 ? "#82b9f3" : "#7dd3fc"; context.lineWidth = major ? 2.4 : 1.65; context.stroke();
        const head = pointAt(progress);
        context.fillStyle = index % 5 === 0 ? "#bfdbfe" : "#e0f2fe"; context.fillRect(head[0] - 1.5, head[1] - 1.5, 3, 3);
      });
    };
    const animate = (time: number) => {
      if (document.hidden || reducedMotion.matches) return;
      if (time - previous >= 33) { draw(time / 1000); previous = time; }
      frame = requestAnimationFrame(animate);
    };
    const updateMotion = () => {
      cancelAnimationFrame(frame);
      draw(0);
      if (!document.hidden && !reducedMotion.matches) frame = requestAnimationFrame(animate);
    };
    const resize = () => {
      const bounds = canvas.getBoundingClientRect(), ratio = Math.min(devicePixelRatio || 1, 2);
      width = bounds.width; height = bounds.height;
      if (!width || !height) return;
      canvas.width = background.width = Math.round(width * ratio); canvas.height = background.height = Math.round(height * ratio);
      context.setTransform(ratio, 0, 0, ratio, 0, 0); base.setTransform(ratio, 0, 0, ratio, 0, 0);
      paths = buildMesh(base, width, height);
      updateMotion();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    reducedMotion.addEventListener("change", updateMotion);
    document.addEventListener("visibilitychange", updateMotion);
    resize();
    return () => {
      cancelAnimationFrame(frame); observer.disconnect();
      reducedMotion.removeEventListener("change", updateMotion);
      document.removeEventListener("visibilitychange", updateMotion);
    };
  }, []);
  return <canvas ref={canvasRef} className="setup-mesh" aria-hidden="true" />;
}

export function SetupShell({ children, compact = false }: { children: ReactNode; compact?: boolean }) {
  return <div className={`setup-screen${compact ? " setup-compact" : ""}`}>
    <MeshBackground />
    <div className="setup-inner">{children}</div>
  </div>;
}