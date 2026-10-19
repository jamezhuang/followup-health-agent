"use client";

import { useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import styles from "./proportional-workspace.module.css";

const DESIGN_WIDTH = 1440;
const DESIGN_HEIGHT = 900;

export default function ProportionalWorkspace({ children }: { children: ReactNode }) {
  const viewport = useRef<HTMLDivElement>(null);
  const [dimensions, setDimensions] = useState({ scale: 1, height: DESIGN_HEIGHT });
  const [ready, setReady] = useState(false);

  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element) return;
    function fit() {
      if (!element) return;
      const { width, height } = element.getBoundingClientRect();
      if (width <= 0 || height <= 0) return;
      // Keep the design width stable and let the logical height fill the viewport.
      // Fitting a fixed aspect ratio left empty bands above and below the app.
      const scale = width / DESIGN_WIDTH;
      setDimensions({ scale, height: height / scale });
      setReady(true);
    }
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const style = {
    "--workspace-scale": dimensions.scale,
    "--workspace-height": `${dimensions.height}px`,
    width: DESIGN_WIDTH,
    height: dimensions.height,
    visibility: ready ? "visible" : "hidden",
  } as CSSProperties;

  return <div ref={viewport} className={styles.viewport}>
    <div className={styles.stage} style={style}>{children}</div>
  </div>;
}
