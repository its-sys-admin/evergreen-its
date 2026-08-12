import { useRef, useState } from "react";
import type { Field, PhotoValue } from "../forms/types";
import { extractExif } from "../lib/exif";

// Site-photo capture for `input: "photo"` header fields (PR-1, 2026-06-12).
//
// D1-INLINE TRANSPORT (owner decision 2026-06-12): each photo is downscaled + re-encoded
// to JPEG in the browser and rides payload_json as base64, so the Worker's canonical
// HMAC covers the bytes with zero signing changes. The canvas re-encode also DROPS EXIF
// — the strip half of "caption then strip"; extractExif reads the caption sidecar
// (taken_at / gps) from the ORIGINAL bytes first. Sidecar strings are display-only and
// untrusted downstream.
//
// TRUST: everything here is UX. The Worker re-enforces shape/bounds (validatePhotoValues)
// and Mac-side §34 screening (PR-2) re-validates bytes before any Box upload or render.
//
// NOTE: no `capture` attribute on the file input — omitting it gives field crews the
// camera/gallery chooser (capture="environment" forces live-camera-only on Android).

// CLIENT per-photo budget (decoded bytes) — deliberately TIGHTER than the Worker's hard cap
// (PHOTO_MAX_BYTES = 400_000, worker/index.ts:518). R3-F2 math: the whole `values` payload must
// fit the Worker's PAYLOAD_MAX = 1_800_000 JSON chars (worker/index.ts:521,:604) and base64
// inflates 3 decoded bytes → 4 chars:
//   400_000 decoded → 533_336 b64 chars → 4 photos = 2_133_344  → deterministic 413
//   280_000 decoded → 373_336 b64 chars → 4 photos = 1_493_344  → ~300KB headroom for the
//                     non-photo values + per-photo JSON envelopes (name/taken_at/gps ≲ 1KB total)
// A submission can still carry up to 8 photos across multiple fields (worker
// PHOTO_MAX_PER_SUBMISSION) — 8 × 373_336 exceeds the cap, so api.submitForm's exact
// pre-payload check (SUBMIT_PAYLOAD_MAX) catches that tail with actionable copy pre-network.
export const PHOTO_MAX_BYTES = 280_000;
const HARD_MAX = 4; // mirror worker PHOTO_MAX_PER_FIELD + publishValidation PHOTO_MAX_COUNT
const LADDER: ReadonlyArray<readonly [number, number]> = [
  [1280, 0.8],
  [1024, 0.7],
  [800, 0.6],
];

/** Effective photo cap for a field: clamp definition max_count into 1..HARD_MAX. */
export function maxCountFor(field: Field): number {
  const m = field.max_count;
  return typeof m === "number" && Number.isInteger(m) && m >= 1 && m <= HARD_MAX ? m : HARD_MAX;
}

/** Append `added` to `existing`, truncating at `max` (pure — unit-tested). */
export function appendPhotos(existing: PhotoValue[], added: PhotoValue[], max: number): PhotoValue[] {
  return [...existing, ...added].slice(0, max);
}

function safeName(raw: string, index: number): string {
  const base = (raw.split(/[\\/]/).pop() ?? "").slice(0, 100);
  return base || `photo-${index}.jpg`;
}

async function fileToImage(file: File): Promise<ImageBitmap | HTMLImageElement> {
  if (typeof createImageBitmap === "function") {
    try {
      // Live camera captures are often portrait with an EXIF orientation tag; honor it
      // so the re-encode isn't sideways. (The re-encode then bakes the rotation in —
      // correct, since it also strips the EXIF that carried the tag.)
      return await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch {
      return createImageBitmap(file); // older engines without the options bag
    }
  }
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("image decode failed"));
    };
    img.src = url;
  });
}

async function reencode(file: File, maxEdge: number, quality: number): Promise<Blob | null> {
  const img = await fileToImage(file);
  const scale = Math.min(1, maxEdge / Math.max(img.width, img.height));
  const w = Math.max(1, Math.round(img.width * scale));
  const h = Math.max(1, Math.round(img.height * scale));
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.drawImage(img, 0, 0, w, h);
  if ("close" in img) img.close();
  return new Promise((resolve) => canvas.toBlob((b) => resolve(b), "image/jpeg", quality));
}

async function blobToBase64(blob: Blob): Promise<string> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result as string);
    r.onerror = () => reject(r.error ?? new Error("read failed"));
    r.readAsDataURL(blob);
  });
  return dataUrl.slice(dataUrl.indexOf(",") + 1);
}

/** One file → PhotoValue (downscale ladder until ≤ PHOTO_MAX_BYTES), or null if unusable. */
export async function encodePhoto(file: File, index: number): Promise<PhotoValue | null> {
  let sidecar = { taken_at: "", gps: "" };
  try {
    sidecar = extractExif(await file.arrayBuffer());
  } catch {
    // display-only sidecar — never block the photo on it
  }
  for (const [edge, quality] of LADDER) {
    let blob: Blob | null = null;
    try {
      blob = await reencode(file, edge, quality);
    } catch {
      return null; // not decodable as an image
    }
    if (blob && blob.size <= PHOTO_MAX_BYTES) {
      return {
        data: await blobToBase64(blob),
        name: safeName(file.name, index),
        taken_at: sidecar.taken_at,
        gps: sidecar.gps,
      };
    }
  }
  return null; // even 800px/q0.6 exceeded the budget — extreme aspect/noise case
}

interface Props {
  field: Field;
  photos: PhotoValue[];
  onChange: (next: PhotoValue[]) => void;
}

export function PhotoField({ field, photos, onChange }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const max = maxCountFor(field);
  const room = Math.max(0, max - photos.length);

  const onFiles = async (list: FileList | null) => {
    if (!list || list.length === 0) return;
    setBusy(true);
    setError("");
    const added: PhotoValue[] = [];
    let failed = 0;
    for (const file of Array.from(list).slice(0, room)) {
      const p = await encodePhoto(file, photos.length + added.length + 1);
      if (p) added.push(p);
      else failed += 1;
    }
    const notes: string[] = [];
    if (list.length > room) notes.push(`Limit is ${max} photo${max === 1 ? "" : "s"} — extra files were skipped.`);
    if (failed > 0) notes.push(`${failed} file${failed === 1 ? "" : "s"} could not be processed as a photo.`);
    setError(notes.join(" "));
    if (added.length > 0) onChange(appendPhotos(photos, added, max));
    setBusy(false);
    if (inputRef.current) inputRef.current.value = "";
  };

  return (
    <div className="field photo-field">
      <span className="field__label">
        {field.label}
        {field.required ? " *" : ""} ({photos.length}/{max})
      </span>
      {photos.length > 0 ? (
        <div className="photo-field__thumbs">
          {photos.map((p, i) => (
            <figure className="photo-field__thumb" key={`${p.name}-${i}`}>
              <img src={`data:image/jpeg;base64,${p.data}`} alt={p.name || `Photo ${i + 1}`} />
              {p.taken_at || p.gps ? (
                <figcaption className="photo-field__caption">
                  {[p.taken_at.replace("T", " "), p.gps].filter(Boolean).join(" · ")}
                </figcaption>
              ) : null}
              <button
                type="button"
                className="photo-field__remove"
                aria-label={`Remove photo ${i + 1}`}
                onClick={() => onChange(photos.filter((_, j) => j !== i))}
              >
                ✕
              </button>
            </figure>
          ))}
        </div>
      ) : null}
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        multiple
        hidden
        data-testid={`photo-input-${field.key}`}
        onChange={(e) => {
          void onFiles(e.target.files);
        }}
      />
      <button
        type="button"
        className="btn btn--secondary"
        disabled={busy || room === 0}
        onClick={() => inputRef.current?.click()}
      >
        {busy ? "Processing…" : room === 0 ? "Photo limit reached" : "+ Add photos"}
      </button>
      {error ? (
        <p className="photo-field__error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
