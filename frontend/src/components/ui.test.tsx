/**
 * The display mask.
 *
 * This app's whole subject is words nobody wants rendered in full, so the UI shows
 * them masked. `maskWord` keeps the first and last character precisely because a
 * review screen still has to be usable -- deciding whether a detection is a false
 * positive means knowing roughly which word fired. The separator set is copied from
 * the backend's `mask_text` (matching/compiler.py) rather than reinvented, so a
 * phrase masks the same way on screen as it does in a redacted subtitle.
 */

import { describe, expect, it } from "vitest";
import { maskIn, maskWord } from "./ui";

describe("maskWord", () => {
  it("keeps the first and last character", () => {
    expect(maskWord("fuck")).toBe("f**k");
    expect(maskWord("shit")).toBe("s**t");
    expect(maskWord("bitch")).toBe("b***h");
    expect(maskWord("motherfucker")).toBe("m**********r");
  });

  it("masks a phrase token by token, leaving separators alone", () => {
    expect(maskWord("son of a bitch")).toBe("s*n o* a b***h");
    expect(maskWord("god-damn")).toBe("g*d-d**n");
    expect(maskWord("fuckin'")).toBe("f****n'");
    expect(maskWord("god damn")).toBe("g*d d**n");
  });

  it("masks a two-letter token down to its first character", () => {
    /** Both of its characters are "first and last", so the rule as written would
     * render it whole -- and nothing may reach the DOM unmasked. It is why the
     * phrase above reads `o*` and not `of`. */
    expect(maskWord("of")).toBe("o*");
    expect(maskWord("a")).toBe("a");
    expect(maskWord("")).toBe("");
  });

  it("leaves a string with no letters untouched", () => {
    expect(maskWord("---")).toBe("---");
    expect(maskWord("  ")).toBe("  ");
  });

  it("treats digits as ordinary characters, so leet needs no special case", () => {
    expect(maskWord("sh1t")).toBe("s**t");
  });

  it("never changes case, and is idempotent", () => {
    expect(maskWord("SHIT")).toBe("S**T");
    expect(maskWord(maskWord("motherfucker"))).toBe(maskWord("motherfucker"));
  });

  it("over-masks rather than under-masks around unlisted punctuation", () => {
    /** A comma is not in the backend's separator set, so it becomes the "last"
     * character and the real last letter is starred. Safe direction; diverging from
     * compiler.py to prettify this would be the more expensive mistake. */
    expect(maskWord("shit,")).toBe("s***,");
  });
});

describe("maskIn", () => {
  it("masks a known term inside prose", () => {
    expect(maskIn("thank god", ["god"])).toBe("thank g*d");
    expect(maskIn("god god", ["god"])).toBe("g*d g*d");
  });

  it("matches whole words only", () => {
    expect(maskIn("Godzilla ate", ["god"])).toBe("Godzilla ate");
    expect(maskIn("`hello`, `shell`, `Michelle` never match", ["hell"])).toBe(
      "`hello`, `shell`, `Michelle` never match",
    );
  });

  it("preserves the matched text's own case and punctuation", () => {
    expect(maskIn("God!", ["god"])).toBe("G*d!");
  });

  it("prefers the longest term, so a phrase beats the word inside it", () => {
    expect(maskIn("a bitch, son of a bitch", ["bitch", "son of a bitch"])).toBe(
      "a b***h, s*n o* a b***h",
    );
  });

  it("matches a phrase term across any separator", () => {
    expect(maskIn("son-of-a-bitch", ["son of a bitch"])).toBe("s*n-o*-a-b***h");
  });

  it("escapes regex metacharacters in a term", () => {
    expect(maskIn("abc", ["a.c"])).toBe("abc");
  });

  it("returns the text untouched when there is nothing to mask", () => {
    expect(maskIn("plain text", [])).toBe("plain text");
    expect(maskIn("plain text", [null, undefined, ""])).toBe("plain text");
  });
});
