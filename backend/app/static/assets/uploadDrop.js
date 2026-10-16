export function bindArchiveDrop(zone, input, environment = {}) {
  if (!zone || !input) return () => {};
  const DataTransferCtor = environment.DataTransfer ?? globalThis.DataTransfer;
  const EventCtor = environment.Event ?? globalThis.Event;
  let dragDepth = 0;
  const prevent = (event) => event.preventDefault();
  const enter = (event) => {
    event.preventDefault();
    dragDepth += 1;
    zone.classList.add("drag-active");
  };
  const over = (event) => {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    zone.classList.add("drag-active");
  };
  const leave = (event) => {
    event.preventDefault();
    if (event.relatedTarget && zone.contains?.(event.relatedTarget)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) zone.classList.remove("drag-active");
  };
  const drop = (event) => {
    event.preventDefault();
    dragDepth = 0;
    zone.classList.remove("drag-active");
    const files = Array.from(event.dataTransfer?.files || []);
    if (!files.length || !DataTransferCtor || !EventCtor) return;
    try {
      const transfer = new DataTransferCtor();
      files.forEach((file) => transfer.items.add(file));
      input.files = transfer.files;
      input.dispatchEvent(new EventCtor("change", { bubbles: true }));
    } catch {
      // File input assignment is restricted in some browsers; the native picker remains available.
      zone.dataset.dropUnavailable = "true";
      zone.classList.add("drop-error");
      const status = zone.querySelector?.(".file-name");
      if (status) status.textContent = "Could not add dropped file. Choose the archive to continue.";
    }
  };
  const handlers = { dragenter: enter, dragover: over, dragleave: leave, drop };
  Object.entries(handlers).forEach(([type, handler]) => zone.addEventListener(type, handler));
  zone.addEventListener("dragend", prevent);
  return () => {
    Object.entries(handlers).forEach(([type, handler]) => zone.removeEventListener(type, handler));
    zone.removeEventListener("dragend", prevent);
    zone.classList.remove("drag-active");
  };
}
