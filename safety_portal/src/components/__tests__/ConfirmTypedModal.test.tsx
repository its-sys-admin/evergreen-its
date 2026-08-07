/**
 * The typed-confirmation dialog (ROADMAP Track 6).
 *
 * This is the SPA's first real modal, and it exists because a `window.confirm` accepts a reflexive
 * Enter — unacceptable for an action that relocates a job's folders across two external systems.
 * What is under test is the ARMING RULE, because every way the button can arm without an exact
 * match is a way to archive the wrong job: partial text, wrong case, stale text from a previous
 * open, and Enter-as-a-shortcut.
 *
 * Note what is NOT claimed here: this dialog is a usability affordance, not the security control.
 * The Worker re-checks `confirm` against the row's own project_name server-side, so a caller who
 * skips the dialog entirely is still refused. These tests cover the affordance behaving as the
 * operator expects, not the boundary.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConfirmTypedModal } from "../ConfirmTypedModal";

afterEach(cleanup);

function setup(over: Partial<Parameters<typeof ConfirmTypedModal>[0]> = {}) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  const utils = render(
    <ConfirmTypedModal
      open
      title="Archive &quot;Coker&quot;?"
      expected="Coker"
      body={<p>what moves</p>}
      confirmLabel="Archive job"
      onConfirm={onConfirm}
      onCancel={onCancel}
      {...over}
    />,
  );
  const input = () => screen.getByLabelText("Confirmation phrase") as HTMLInputElement;
  const confirm = () => screen.getByRole("button", { name: /archive job/i }) as HTMLButtonElement;
  return { onConfirm, onCancel, input, confirm, ...utils };
}

describe("ConfirmTypedModal — the arming rule", () => {
  it("starts disarmed with an empty field", () => {
    const { confirm } = setup();
    expect(confirm().disabled).toBe(true);
  });

  it("stays disarmed on a partial match", () => {
    const { input, confirm } = setup();
    fireEvent.change(input(), { target: { value: "Cok" } });
    expect(confirm().disabled).toBe(true);
  });

  it("arms only on the exact phrase", () => {
    const { input, confirm } = setup();
    fireEvent.change(input(), { target: { value: "Coker" } });
    expect(confirm().disabled).toBe(false);
  });

  it("is case-SENSITIVE — the same rule the worker applies", () => {
    // If the button armed on "coker" while the server compares case-sensitively, the operator
    // would face a live button that 409s. The two must agree.
    const { input, confirm } = setup();
    fireEvent.change(input(), { target: { value: "coker" } });
    expect(confirm().disabled).toBe(true);
  });

  it("trims surrounding whitespace, and nothing else", () => {
    const { input, confirm } = setup();
    fireEvent.change(input(), { target: { value: "  Coker  " } });
    expect(confirm().disabled).toBe(false);
  });

  it("does not treat an interior space as trimmable", () => {
    const { input, confirm } = setup({ expected: "Bradley Solar" });
    fireEvent.change(input(), { target: { value: "BradleySolar" } });
    expect(confirm().disabled).toBe(true);
  });

  it("fires onConfirm when the armed button is pressed", () => {
    const { input, confirm, onConfirm } = setup();
    fireEvent.change(input(), { target: { value: "Coker" } });
    fireEvent.click(confirm());
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("Enter on a PARTIAL match is a no-op, not a shortcut past the confirmation", () => {
    // The whole reason this modal exists instead of window.confirm.
    const { input, onConfirm } = setup();
    fireEvent.change(input(), { target: { value: "Cok" } });
    fireEvent.submit(input().closest("form")!);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("Enter on an exact match does confirm", () => {
    const { input, onConfirm } = setup();
    fireEvent.change(input(), { target: { value: "Coker" } });
    fireEvent.submit(input().closest("form")!);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});

describe("ConfirmTypedModal — state that must not leak between opens", () => {
  it("clears the field on reopen so a previous attempt cannot pre-arm the button", () => {
    // Without the clear, closing a modal mid-type and reopening it on a DIFFERENT job would
    // present an already-armed Archive button carrying the previous job's phrase.
    const { input, confirm, rerender } = setup();
    fireEvent.change(input(), { target: { value: "Coker" } });
    expect(confirm().disabled).toBe(false);

    rerender(
      <ConfirmTypedModal
        open={false}
        title="x"
        expected="Coker"
        body={null}
        confirmLabel="Archive job"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    rerender(
      <ConfirmTypedModal
        open
        title="x"
        expected="Coker"
        body={null}
        confirmLabel="Archive job"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect((screen.getByLabelText("Confirmation phrase") as HTMLInputElement).value).toBe("");
    expect((screen.getByRole("button", { name: /archive job/i }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("ConfirmTypedModal — busy and dismissal", () => {
  it("disarms while busy even on an exact match, so a double-press cannot double-submit", () => {
    const { input } = setup();
    fireEvent.change(input(), { target: { value: "Coker" } });
    cleanup();
    render(
      <ConfirmTypedModal
        open
        busy
        title="x"
        expected="Coker"
        body={null}
        confirmLabel="Archive job"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect((screen.getByRole("button", { name: /working/i }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("Escape cancels", () => {
    const { onCancel } = setup();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onCancel).toHaveBeenCalled();
  });

  it("renders nothing at all when closed", () => {
    const { container } = setup({ open: false });
    expect(container.firstChild).toBeNull();
  });
});
