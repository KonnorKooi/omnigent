import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { OttoIcon } from "./OttoIcon";

afterEach(cleanup);

describe("OttoIcon", () => {
  it("exposes two otto-eye groups for the blink animation", () => {
    const { container } = render(<OttoIcon />);
    // The blink keyframes target `.otto-working .otto-eye` in index.css; CSS
    // selectors fail silently, so renaming/flattening these groups would
    // freeze the eyes with no other signal.
    const eyes = container.querySelectorAll("svg > g.otto-eye");
    // 2 = Otto's eye + the joey's (both drawn in profile).
    expect(eyes).toHaveLength(2);
    // 2 rects per eye = white + pupil; losing one shifts the group's fill-box
    // bounds and the blink no longer collapses on center.
    for (const eye of eyes) {
      expect(eye.querySelectorAll("rect")).toHaveLength(2);
    }
  });

  it("wraps every pupil in an otto-pupil group for cursor tracking", () => {
    const { container } = render(<OttoIcon />);
    // OttoEyes finds these groups by class through the forwarded ref and pairs
    // them with EYE_CENTERS by index; querySelectorAll fails silently, so a
    // rename or count change would break or skew tracking.
    const pupils = container.querySelectorAll("svg g.otto-eye > g.otto-pupil");
    expect(pupils).toHaveLength(2);
    // The white must stay outside the group, or it would slide along with the
    // pupil instead of framing it.
    for (const pupil of pupils) {
      expect(pupil.querySelectorAll("rect")).toHaveLength(1);
    }
  });

  it("spreads props onto the root svg and stays hidden from screen readers", () => {
    const { container } = render(<OttoIcon className="otto-working h-4" />);
    const svg = container.querySelector("svg");
    // The animation is opt-in via className, so the spread must reach the root.
    expect(svg).toHaveClass("otto-working");
    // The art's coordinate space; consumers size via className so a viewBox
    // change silently distorts the mascot everywhere.
    expect(svg).toHaveAttribute("viewBox", "0 0 48 48");
    // Decorative by default; the pin's aria-live region must only
    // ever announce the "Working…" text.
    expect(svg).toHaveAttribute("aria-hidden", "true");
  });

  it("lets callers override aria-hidden for the new-chat hero render", () => {
    const { container } = render(<OttoIcon role="img" aria-label="Omnigent" aria-hidden={false} />);
    const svg = container.querySelector("svg");
    // NewChatDialog renders the mascot as a meaningful image; the override
    // only works while the spread stays after the aria-hidden default.
    expect(svg).toHaveAttribute("aria-hidden", "false");
    expect(svg).toHaveAttribute("aria-label", "Omnigent");
  });
});
