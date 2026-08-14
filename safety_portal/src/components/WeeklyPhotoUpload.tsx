import { useCallback, useEffect, useRef, useState } from "react";
import { encodePhoto } from "./PhotoField";
import { uploadDailyPhoto, listDailyPhotos } from "../lib/fieldops_daily_photos";
import { ApiError } from "../lib/errorCopy";
import { weekEndFor } from "../lib/fieldops_report";

// Office photo upload for the weekly report (0074, Track B-C). A SIBLING of the field's
// AdditionalPhotosSection, deliberately not a modification of it: the field component is
// per-day, claim-bearing and submission-ref-driven — none of which applies here. This one is
// WEEK-scoped (the office picks which day of the Sat→Fri week a photo belongs to; work_date is
// what the WPR windows on), uploads with origin:'office_wpr' (90d retention instead of the
// field flow's 7d), and never claims — the photo becomes WPR-offerable when the Mac's screening
// pass flips it clean (~1 poll cycle, ≈60s), at which point `onPoolChanged` tells the page to
// re-fetch its offered list.
//
// No bytes are ever previewed pre-screening (the record-only posture holds until the screened
// thumbnail exists); an upload shows as a "Screening…" ghost card until it resolves.

const POLL_MS = 15_000;

type OwnUpload = {
  pool_id: number;
  work_date: string;
  status: "pending" | "clean" | "refused";
};

function dayLabel(iso: string): string {
  const d = new Date(`${iso}T12:00:00Z`);
  const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()];
  return `${wd} ${d.getUTCMonth() + 1}/${d.getUTCDate()}`;
}

function weekDays(weekStart: string): string[] {
  const out: string[] = [];
  const d = new Date(`${weekStart}T00:00:00Z`);
  for (let i = 0; i < 7; i += 1) {
    out.push(d.toISOString().slice(0, 10));
    d.setUTCDate(d.getUTCDate() + 1);
  }
  return out;
}

export function WeeklyPhotoUpload(props: {
  jobId: string;
  weekStart: string;
  onPoolChanged: () => void;
}) {
  const { jobId, weekStart, onPoolChanged } = props;
  const days = weekDays(weekStart);
  const weekEnd = weekEndFor(weekStart);
  const today = new Date().toISOString().slice(0, 10);
  const defaultDay = today >= weekStart && today <= weekEnd ? today : weekEnd;

  const [day, setDay] = useState(defaultDay);
  const [uploads, setUploads] = useState<OwnUpload[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // A week flip resets the day selector (and the ghost list belongs to the old week's poll).
  useEffect(() => {
    setDay(today >= weekStart && today <= weekEnd ? today : weekEnd);
    setUploads([]);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps — derived from weekStart only
  }, [weekStart]);

  const pending = uploads.filter((u) => u.status === "pending");

  // Poll the screening status of our own pending uploads (one list read per distinct day).
  const poll = useCallback(async () => {
    const daysToCheck = [...new Set(pending.map((u) => u.work_date))];
    if (daysToCheck.length === 0) return;
    let flipped = false;
    for (const d of daysToCheck) {
      try {
        const rows = await listDailyPhotos(jobId, d);
        // Decide the flip from the FETCHED rows + the current pending snapshot — never inside
        // the setState updater, which React runs lazily (a flag set there is unread by the
        // time we check it; caught by the component test).
        if (pending.some((u) => u.work_date === d && rows.some((r) => r.id === u.pool_id && r.status === "clean"))) {
          flipped = true;
        }
        setUploads((prev) =>
          prev.map((u) => {
            const row = rows.find((r) => r.id === u.pool_id);
            if (!row || row.status === u.status) return u;
            return { ...u, status: row.status as OwnUpload["status"] };
          }),
        );
      } catch {
        // A transient list failure just delays the chip flip — the next tick retries.
      }
    }
    if (flipped) onPoolChanged();
  }, [jobId, pending, onPoolChanged]);

  useEffect(() => {
    if (pending.length === 0) return;
    const t = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(t);
  }, [pending.length, poll]);

  const onFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      for (let i = 0; i < files.length; i += 1) {
        const photo = await encodePhoto(files[i], i + 1);
        if (!photo) {
          setError("That file could not be read as a photo.");
          continue;
        }
        try {
          const res = await uploadDailyPhoto(jobId, day, photo, { origin: "office_wpr" });
          setUploads((prev) => [...prev, { pool_id: res.pool_id, work_date: day, status: "pending" }]);
        } catch (e) {
          if (e instanceof ApiError && e.code === "pool_cap_reached") {
            setError("The day's photo pool is full — pick another day or remove some photos.");
          } else if (e instanceof ApiError && e.code === "pool_backlogged") {
            setError("Screening is backed up right now. Try again in a few minutes.");
          } else {
            setError("Upload failed. Check the connection and try again.");
          }
          break;
        }
      }
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <div className="wpr-upload">
      <div className="wpr-upload__row">
        <label className="wpr-upload__day">
          <span>Photo day</span>
          <select
            aria-label="Day the added photos were taken"
            value={day}
            onChange={(e) => setDay(e.target.value)}
          >
            {days.map((d) => (
              <option key={d} value={d}>{dayLabel(d)}</option>
            ))}
          </select>
        </label>
        <label className={`btn btn--secondary wpr-upload__btn${busy ? " is-busy" : ""}`}>
          {busy ? "Adding…" : "Add photos"}
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            multiple
            disabled={busy}
            aria-label="Add photos to this week's pool"
            onChange={(e) => void onFiles(e.target.files)}
          />
        </label>
      </div>
      {error && <p className="wpr-upload__error" role="alert">{error}</p>}
      {uploads.length > 0 && (
        <ul className="wpr-upload__chips">
          {uploads.map((u) => (
            <li
              key={u.pool_id}
              // Static class lookup, not a template suffix: the phantom-CSS guard (rightly)
              // cannot verify a dynamic class name, and 'pending' rides the base chip style.
              className={
                u.status === "clean"
                  ? "wpr-upload__chip wpr-upload__chip--clean"
                  : u.status === "refused"
                    ? "wpr-upload__chip wpr-upload__chip--refused"
                    : "wpr-upload__chip"
              }
            >
              {dayLabel(u.work_date)} —{" "}
              {u.status === "pending" ? "Screening…" : u.status === "clean" ? "In the pool ✓" : "Refused by screening"}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
