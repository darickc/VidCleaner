/**
 * PLAN.md §9.5 — categories with word chips, custom words, the whitelist, and the
 * profile editor.
 *
 * The page is organised around the thing that is easy to get wrong: **which words are
 * actually being muted right now**. So the chips are the primary object, they show
 * their enabled state directly, and the reason a built-in ships disabled (its YAML
 * `note`) is on the chip's tooltip rather than hidden in a file — 50 of the 181 shipped
 * entries are off for precision, and "why isn't `cock` muted?" should be answerable by
 * hovering it.
 *
 * A profile decides which *categories* apply; `enabled` is global to the word. That
 * split is the schema's, not a simplification: `word_entries.enabled` has one row per
 * canonical, and `profiles.categories_json` is per profile. The copy says so, because
 * otherwise the two controls look like they do the same thing.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  createProfile,
  createWord,
  deleteProfile,
  deleteWhitelist,
  deleteWord,
  getProfiles,
  getWhitelist,
  getWords,
  patchProfile,
  patchWord,
} from "../api/client";
import type { Profile, WordRow } from "../api/types";
import { Page } from "../components/Page";
import { Badge, Button, Card, Empty, ErrorNote } from "../components/ui";

const INPUT =
  "rounded border border-slate-800 bg-slate-900/60 px-2 py-1 text-sm outline-none focus:border-slate-600";

/** Every write here changes the mute set, so they all invalidate the same three keys. */
function useWordsMutation<TArgs>(fn: (args: TArgs) => Promise<unknown>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      for (const key of ["words", "profiles", "whitelist"]) {
        client.invalidateQueries({ queryKey: [key] });
      }
    },
  });
}

function WordChip({
  word,
  onToggle,
  onDelete,
  busy,
}: {
  word: WordRow;
  onToggle: () => void;
  onDelete: () => void;
  busy: boolean;
}) {
  const title = [
    word.forms.length > 1 ? `forms: ${word.forms.join(", ")}` : null,
    word.note ? `note: ${word.note}` : null,
    word.focus.length ? `mutes only: ${word.focus.join(", ")}` : null,
    word.parent ? `part of ${word.parent}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <span className="inline-flex items-center gap-1">
      <button
        type="button"
        onClick={onToggle}
        disabled={busy || word.id === null}
        title={title || undefined}
        aria-pressed={word.enabled}
        aria-label={`${word.canonical}${word.enabled ? " (muted)" : " (not muted)"}`}
        className={`rounded px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset transition disabled:opacity-40 ${
          word.enabled
            ? "bg-sky-500/15 text-sky-200 ring-sky-500/30 hover:bg-sky-500/25"
            : "bg-slate-800/60 text-slate-500 ring-slate-700 hover:bg-slate-800"
        }`}
      >
        {word.canonical}
        {word.is_phrase && <span className="ml-1 opacity-60">⋯</span>}
      </button>
      {!word.is_builtin && (
        <button
          type="button"
          onClick={onDelete}
          disabled={busy}
          aria-label={`Delete ${word.canonical}`}
          className="text-xs text-slate-600 hover:text-rose-400"
        >
          ×
        </button>
      )}
    </span>
  );
}

function CategoryCard({
  category,
  words,
  active,
  profileName,
  counts,
}: {
  category: string;
  words: WordRow[];
  active: boolean;
  profileName: string;
  counts: { total: number; enabled: number };
}) {
  const toggle = useWordsMutation(({ id, enabled }: { id: number; enabled: boolean }) =>
    patchWord(id, enabled),
  );
  const remove = useWordsMutation((id: number) => deleteWord(id));

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          {category}
          <Badge tone={active ? "ok" : "idle"}>
            {active ? `in ${profileName}` : `not in ${profileName}`}
          </Badge>
          <span className="text-xs normal-case text-slate-500">
            {counts.enabled} of {counts.total} on
          </span>
        </span>
      }
    >
      <div className="flex flex-wrap gap-2">
        {words.map((word) => (
          <WordChip
            key={word.canonical}
            word={word}
            busy={toggle.isPending || remove.isPending}
            onToggle={() =>
              word.id !== null && toggle.mutate({ id: word.id, enabled: !word.enabled })
            }
            onDelete={() => word.id !== null && remove.mutate(word.id)}
          />
        ))}
      </div>
      <ErrorNote error={toggle.error ?? remove.error} />
    </Card>
  );
}

function AddWord({ categories }: { categories: string[] }) {
  const [canonical, setCanonical] = useState("");
  const [category, setCategory] = useState(categories[0] ?? "strong");
  const [forms, setForms] = useState("");
  const create = useWordsMutation(
    (body: { canonical: string; category: string; forms: string[] }) => createWord(body),
  );

  const submit = () => {
    create.mutate(
      {
        canonical: canonical.trim(),
        category,
        forms: forms
          .split(",")
          .map((f) => f.trim())
          .filter(Boolean),
      },
      {
        onSuccess: () => {
          setCanonical("");
          setForms("");
        },
      },
    );
  };

  return (
    <Card title="Add a word or phrase">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          Word or phrase
          <input
            aria-label="Word or phrase"
            className={INPUT}
            value={canonical}
            onChange={(e) => setCanonical(e.target.value)}
            placeholder="frak"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          Category
          <select
            aria-label="Category"
            className={INPUT}
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          >
            {categories.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-1 flex-col gap-1 text-xs text-slate-400">
          Other forms, comma separated
          <input
            aria-label="Other forms, comma separated"
            className={INPUT}
            value={forms}
            onChange={(e) => setForms(e.target.value)}
            placeholder="fraks, fraking"
          />
        </label>
        <Button variant="primary" onClick={submit} disabled={!canonical.trim() || create.isPending}>
          Add
        </Button>
      </div>
      <p className="mt-2 text-xs text-slate-500">
        List the inflections you want matched. They are matched literally, never by suffix rules —
        “fuck” does not imply “fucking”, and a rule that guessed would also match “shiitake”. A
        phrase (any space in the word) may be separated by spaces, hyphens or apostrophes but never
        a line break.
      </p>
      <ErrorNote error={create.error} />
    </Card>
  );
}

function ProfileEditor({
  profiles,
  categories,
  selected,
  onSelect,
}: {
  profiles: Profile[];
  categories: string[];
  selected: Profile;
  onSelect: (id: number) => void;
}) {
  const [name, setName] = useState("");

  const save = useWordsMutation(({ id, body }: { id: number; body: Partial<Profile> }) =>
    patchProfile(id, body),
  );
  const add = useWordsMutation((body: { name: string }) => createProfile(body));
  const remove = useWordsMutation((id: number) => deleteProfile(id));

  const toggleCategory = (category: string) => {
    const next = selected.categories.includes(category)
      ? selected.categories.filter((c) => c !== category)
      : [...selected.categories, category];
    save.mutate({ id: selected.id, body: { categories: next } });
  };

  return (
    <Card
      title="Profiles"
      actions={
        profiles.length > 1 ? (
          <select
            aria-label="Profile"
            className={INPUT}
            value={selected.id}
            onChange={(e) => onSelect(Number(e.target.value))}
          >
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
                {p.is_default ? " (default)" : ""}
              </option>
            ))}
          </select>
        ) : undefined
      }
    >
      <div className="space-y-4 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <strong className="text-slate-200">{selected.name}</strong>
          {selected.is_default ? (
            <Badge tone="ok">default</Badge>
          ) : (
            <Button
              onClick={() => save.mutate({ id: selected.id, body: { is_default: true } })}
              disabled={save.isPending}
            >
              Make default
            </Button>
          )}
          {selected.titles > 0 && (
            <Badge>
              {selected.titles} title{selected.titles === 1 ? "" : "s"}
            </Badge>
          )}
          {!selected.is_default && (
            <Button
              variant="danger"
              onClick={() => remove.mutate(selected.id)}
              disabled={remove.isPending}
            >
              Delete
            </Button>
          )}
        </div>

        <div>
          <p className="mb-1 text-xs tracking-wide text-slate-400 uppercase">Categories</p>
          <div className="flex flex-wrap gap-2">
            {categories.map((category) => {
              const on = selected.categories.includes(category);
              return (
                <button
                  key={category}
                  type="button"
                  onClick={() => toggleCategory(category)}
                  disabled={save.isPending}
                  aria-pressed={on}
                  aria-label={`${category} in ${selected.name}`}
                  className={`rounded px-2 py-1 text-xs font-medium ring-1 ring-inset transition disabled:opacity-40 ${
                    on
                      ? "bg-sky-500/15 text-sky-200 ring-sky-500/30"
                      : "bg-slate-800/60 text-slate-500 ring-slate-700"
                  }`}
                >
                  {category}
                </button>
              );
            })}
          </div>
          <p className="mt-2 text-xs text-slate-500">
            Categories are per profile; a word’s own on/off switch above is global. So a profile can
            drop “mild” wholesale, and turning a single word off stops it being muted anywhere.
          </p>
        </div>

        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-slate-400">
            Padding before (ms)
            <input
              type="number"
              aria-label="Padding before (ms)"
              className={`${INPUT} w-28`}
              defaultValue={selected.pad_pre_ms}
              key={`pre-${selected.id}-${selected.pad_pre_ms}`}
              onBlur={(e) =>
                Number(e.target.value) !== selected.pad_pre_ms &&
                save.mutate({
                  id: selected.id,
                  body: { pad_pre_ms: Number(e.target.value) },
                })
              }
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-slate-400">
            Padding after (ms)
            <input
              type="number"
              aria-label="Padding after (ms)"
              className={`${INPUT} w-28`}
              defaultValue={selected.pad_post_ms}
              key={`post-${selected.id}-${selected.pad_post_ms}`}
              onBlur={(e) =>
                Number(e.target.value) !== selected.pad_post_ms &&
                save.mutate({
                  id: selected.id,
                  body: { pad_post_ms: Number(e.target.value) },
                })
              }
            />
          </label>
          <p className="flex-1 text-xs text-slate-500">
            Recognition tends to place a word late, so the default pads more after (120 ms) than
            before (80 ms).
          </p>
        </div>

        <div className="flex flex-wrap items-end gap-3 border-t border-slate-800 pt-3">
          <label className="flex flex-col gap-1 text-xs text-slate-400">
            New profile
            <input
              aria-label="New profile"
              className={INPUT}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Strict"
            />
          </label>
          <Button
            onClick={() => add.mutate({ name: name.trim() }, { onSuccess: () => setName("") })}
            disabled={!name.trim() || add.isPending}
          >
            Create
          </Button>
          <p className="flex-1 text-xs text-slate-500">
            Assign a profile to a series or movie from its own page.
          </p>
        </div>

        <ErrorNote error={save.error ?? add.error ?? remove.error} />
      </div>
    </Card>
  );
}

function WhitelistCard() {
  const { data } = useQuery({ queryKey: ["whitelist"], queryFn: getWhitelist });
  const remove = useWordsMutation((id: number) => deleteWhitelist(id));

  return (
    <Card title="Whitelist">
      <p className="mb-3 text-xs text-slate-500">
        Words left audible despite being on a list above. The narrowest scope wins, so a{" "}
        <em>mute it anyway</em> rule on one file or title overrides a broader exception. Add these
        from a detection on the Item page, where the word and the file are already known.
      </p>
      {!data || data.length === 0 ? (
        <Empty>Nothing whitelisted.</Empty>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-xs tracking-wide text-slate-500 uppercase">
            <tr>
              <th className="py-1 text-left">Word</th>
              <th className="py-1 text-left">Scope</th>
              <th className="py-1 text-left">Effect</th>
              <th className="py-1 text-left">Only when it says</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.map((row) => (
              <tr key={row.id} className="border-t border-slate-800/60">
                <td className="py-1 font-medium text-slate-200">{row.canonical_word}</td>
                <td className="py-1 text-slate-400">
                  {row.scope === "global" ? "everywhere" : (row.label ?? row.scope)}
                </td>
                <td className="py-1">
                  <Badge tone={row.mode === "allow" ? "warn" : "idle"}>
                    {row.mode === "allow" ? "mute anyway" : "leave audible"}
                  </Badge>
                </td>
                <td className="py-1 text-slate-500">{row.context_text ?? "—"}</td>
                <td className="py-1 text-right">
                  <Button
                    onClick={() => remove.mutate(row.id)}
                    disabled={remove.isPending}
                    aria-label={`Remove ${row.canonical_word} from the whitelist`}
                  >
                    Remove
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <ErrorNote error={remove.error} />
    </Card>
  );
}

export function WordsPage() {
  const words = useQuery({ queryKey: ["words"], queryFn: getWords });
  const profiles = useQuery({ queryKey: ["profiles"], queryFn: getProfiles });
  // The page has one "profile in view", shared by the editor and the category badges
  // above it. Keeping it inside the editor left the badges describing the *default*
  // while the editor described something else -- caught by clicking through this
  // against a live backend, not by a test.
  const [selectedId, setSelectedId] = useState<number | null>(null);

  if (words.error) {
    return (
      <Page title="Words & Profiles">
        <ErrorNote error={words.error} />
      </Page>
    );
  }
  if (!words.data || !profiles.data) {
    return <Page title="Words & Profiles">Loading…</Page>;
  }

  const selected =
    profiles.data.find((p) => p.id === selectedId) ??
    profiles.data.find((p) => p.is_default) ??
    profiles.data[0];
  if (!selected) return <Page title="Words & Profiles">No profiles.</Page>;
  const byCategory = words.data.categories.map((category) => ({
    category,
    words: words.data.words.filter((w) => w.category === category),
  }));

  return (
    <Page title="Words & Profiles" subtitle="What gets muted, and which titles use which rules.">
      <div className="space-y-4">
        <ProfileEditor
          profiles={profiles.data}
          categories={words.data.categories}
          selected={selected}
          onSelect={setSelectedId}
        />
        <AddWord categories={words.data.categories} />
        {byCategory.map(({ category, words: rows }) => (
          <CategoryCard
            key={category}
            category={category}
            words={rows}
            active={selected.categories.includes(category)}
            profileName={selected.name}
            counts={{
              total: words.data.counts[category] ?? 0,
              enabled: words.data.enabled_counts[category] ?? 0,
            }}
          />
        ))}
        <WhitelistCard />
      </div>
    </Page>
  );
}
