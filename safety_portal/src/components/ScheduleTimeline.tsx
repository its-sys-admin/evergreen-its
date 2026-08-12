import type { CSSProperties } from "react";
import type { ScheduleTaskRow } from "../lib/fieldops_schedules";
import {
  barGeometry,
  isLate,
  progressBar,
  timelineDomain,
  type ScheduleGroup,
} from "../lib/schedule_view";

// The schedule TIMELINE — the page's signature view.
//
// A construction schedule's native form is a bar of time; rendering it as a spreadsheet
// throws away the one thing it exists to show. Here the horizontal axis IS the job's date
// span, so two tasks that overlap look overlapping and a task that runs long looks long.
//
// Each bar carries plan AND actual in one mark: the bordered track is the planned extent
// (start → finish, finish inclusive), the solid fill is percent done. A milestone has no
// duration, so it is drawn as a point — a diamond, the way a drawing set marks one.
//
// GOLD IS THE NOW-MARKER. The portal's existing signature element is the day-rail
// (.fr__guidance--rail), a BRG rail down the daily SOP with a gold tick on the phase of the
// day you are in. This is that device at project scale, so gold means the same thing in both
// places: where you are in time. Nothing else on the page is gold. Because gold-on-white
// fails the contrast floor (tokens.css), the line's MEANING is carried by the BRG "TODAY"
// flag beside it — a viewer who cannot resolve the gold loses no information.
//
// Pure CSS grid + absolute positioning: the project depends on react and react-dom only, and
// a charting library would be a far heavier answer than this question deserves.

/** Bar/point position is handed to CSS as custom properties rather than left/width so the
 *  stylesheet keeps ownership of how a bar is drawn. */
type BarVars = CSSProperties & { "--x"?: string; "--w"?: string };

export function ScheduleTimeline({
  groups,
  today,
}: {
  groups: ScheduleGroup[];
  today: string;
}) {
  const all = groups.flatMap((g) => g.tasks);
  const domain = timelineDomain(all, today);

  if (domain === null) {
    return (
      <div className="sched-tl">
        <div className="sched-empty">
          <p className="sched-empty__title">No dates to plot</p>
          <p>
            None of these tasks carry a start or finish date, so there is nothing to place on a
            timeline. Switch to the list view to see them.
          </p>
        </div>
      </div>
    );
  }

  const todayVars: BarVars | undefined =
    domain.todayPct === null ? undefined : { "--x": `${domain.todayPct}%` };

  return (
    <div className="sched-tl">
      <div className="sched-tl__inner">
        {/* Ruler: month boundaries only. Hairlines — the bars are the ink. */}
        <div className="sched-tl__row" aria-hidden="true">
          <div className="sched-tl__name" />
          <div className="sched-tl__ruler">
            {domain.ticks.map((t) => (
              <span key={t.label} className="sched-tl__tick" style={{ "--x": `${t.pct}%` } as BarVars}>
                {t.label}
              </span>
            ))}
            {todayVars && <span className="sched-tl__today" style={todayVars} />}
            {todayVars && (
              <span className="sched-tl__today-flag" style={todayVars}>
                Today
              </span>
            )}
          </div>
        </div>

        {groups.map((group) => (
          <div key={group.name ?? "__none"}>
            <div className="sched-tl__row sched-tl__row--section">
              <div className="sched-tl__name">{group.name ?? "Tasks"}</div>
              <div className="sched-tl__canvas">
                {todayVars && <span className="sched-tl__today" style={todayVars} />}
              </div>
            </div>

            {group.tasks.map((t) => (
              <TimelineRow key={t.id} task={t} domain={domain} today={today} todayVars={todayVars} />
            ))}
          </div>
        ))}
      </div>

      <div className="sched-tl__legend">
        <span className="sched-tl__legend-key">
          <span className="sched-tl__legend-swatch" /> Planned
        </span>
        <span className="sched-tl__legend-key">
          <span className="sched-tl__legend-swatch sched-tl__legend-swatch--done" /> Work done
        </span>
        <span className="sched-tl__legend-key">
          <span className="sched-tl__legend-swatch sched-tl__legend-swatch--late" /> Past its finish
          date
        </span>
        <span className="sched-tl__legend-key">
          <span className="sched-tl__legend-swatch sched-tl__legend-swatch--today" /> Today
        </span>
      </div>
    </div>
  );
}

function TimelineRow({
  task,
  domain,
  today,
  todayVars,
}: {
  task: ScheduleTaskRow;
  domain: ReturnType<typeof timelineDomain> & object;
  today: string;
  todayVars: BarVars | undefined;
}) {
  const geo = barGeometry(task, domain);
  const late = isLate(task, today);

  // The bar is decorative once the row already states the same facts in text: the sticky name
  // cell names the task and this title spells out the span and the progress, so a screen
  // reader gets the row without having to interpret a rectangle.
  const label = `${task.name} — ${task.start_date ?? "no start"} to ${
    task.finish_date ?? "no finish"
  }, ${progressBar(task.percent_done)}${late ? ", past its finish date" : ""}`;

  return (
    <div className="sched-tl__row">
      <div className="sched-tl__name" title={task.name}>
        {task.name}
      </div>
      <div className="sched-tl__canvas">
        {todayVars && <span className="sched-tl__today" style={todayVars} />}
        {geo === null ? null : geo.point ? (
          <span
            className={`sched-tl__diamond${task.percent_done >= 100 ? " sched-tl__diamond--done" : ""}${
              late ? " sched-tl__diamond--late" : ""
            }`}
            style={{ "--x": `${geo.leftPct}%` } as BarVars}
            title={label}
          />
        ) : (
          <span
            className={`sched-tl__bar${late ? " sched-tl__bar--late" : ""}`}
            style={{ "--x": `${geo.leftPct}%`, "--w": `${geo.widthPct}%` } as BarVars}
            title={label}
          >
            <span
              className="sched-tl__bar-fill"
              style={{ width: `${Math.max(0, Math.min(100, task.percent_done))}%` }}
            />
          </span>
        )}
      </div>
    </div>
  );
}
