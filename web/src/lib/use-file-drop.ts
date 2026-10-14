import { useRef, useState, type DragEvent } from "react";

/** Make an element a drop target for one file: `dragging` while a file is over it, `onFile` with the dropped one. */
export function useFileDrop(onFile: (file: File) => void, enabled = true) {
  const [dragging, setDragging] = useState(false);
  // dragenter and dragleave fire for every child the pointer crosses; count them so leaving a child is not leaving.
  const depth = useRef(0);
  const hasFile = (e: DragEvent) => enabled && Array.from(e.dataTransfer.types).includes("Files");
  return {
    dragging,
    dropProps: {
      onDragEnter: (e: DragEvent) => {
        if (!hasFile(e)) return;
        e.preventDefault();
        depth.current += 1;
        setDragging(true);
      },
      onDragOver: (e: DragEvent) => {
        if (!hasFile(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
      },
      onDragLeave: (e: DragEvent) => {
        if (!hasFile(e)) return;
        depth.current = Math.max(0, depth.current - 1);
        if (depth.current === 0) setDragging(false);
      },
      onDrop: (e: DragEvent) => {
        if (!hasFile(e)) return;
        e.preventDefault();
        depth.current = 0;
        setDragging(false);
        const file = e.dataTransfer.files[0];
        if (file) onFile(file);
      },
    },
  };
}
