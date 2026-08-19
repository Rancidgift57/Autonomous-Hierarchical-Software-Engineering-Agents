"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { api, ApiError } from "@/lib/api";
import { PageHeader } from "@/components/PageHeader";

export default function NewProjectPage() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [ideaPrompt, setIdeaPrompt] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (!name.trim() || !ideaPrompt.trim()) {
      setError("Name and idea prompt are required.");
      return;
    }

    setSubmitting(true);
    try {
      const project = await api.createProject({
        name: name.trim(),
        description: description.trim(),
        idea_prompt: ideaPrompt.trim(),
        repo_url: repoUrl.trim() || null,
      });
      router.push(`/projects/${project.project_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to create project.");
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-2xl px-6 py-10">
      <PageHeader
        eyebrow="AHSEA Control Plane"
        title="New project"
        description="Describe what you want built. The CTO agent turns this into requirements, architecture, and a task DAG for the managers and workers."
      />

      <form onSubmit={handleSubmit} className="flex flex-col gap-5">
        <Field label="Name" required>
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Customer feedback portal"
            maxLength={200}
            disabled={submitting}
          />
        </Field>

        <Field label="Description" hint="Optional short summary shown on the project list.">
          <textarea
            className="input min-h-[80px] resize-y"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="One or two sentences about what this project is."
            maxLength={5000}
            disabled={submitting}
          />
        </Field>

        <Field
          label="Idea prompt"
          required
          hint="The natural-language brief the CTO agent plans from. This is stored with the project — avoid pasting secrets or credentials here."
        >
          <textarea
            className="input min-h-[160px] resize-y"
            value={ideaPrompt}
            onChange={(e) => setIdeaPrompt(e.target.value)}
            placeholder="Describe the product, its users, and the outcomes you need…"
            maxLength={20000}
            disabled={submitting}
          />
        </Field>

        <Field label="Repository URL" hint="Optional. Where agents should push generated code.">
          <input
            className="input"
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            placeholder="https://github.com/org/repo"
            disabled={submitting}
          />
        </Field>

        {error && (
          <p className="rounded-md border border-signal-rose/30 bg-signal-rose/[0.08] px-3 py-2 text-xs text-signal-rose">
            {error}
          </p>
        )}

        <div className="flex items-center gap-3 pt-2">
          <button
            type="submit"
            disabled={submitting}
            className="rounded-md bg-signal-teal px-4 py-2 text-sm font-medium text-base transition hover:bg-signal-teal/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? "Creating…" : "Create project"}
          </button>
          <Link href="/projects" className="text-sm text-ink-muted transition hover:text-ink">
            Cancel
          </Link>
        </div>
      </form>

      <style jsx global>{`
        .input {
          width: 100%;
          border-radius: 0.5rem;
          border: 1px solid #242a33;
          background-color: #12151b;
          padding: 0.5rem 0.75rem;
          font-size: 0.875rem;
          color: #e7eaee;
          outline: none;
        }
        .input::placeholder {
          color: #5b6472;
        }
        .input:focus {
          border-color: rgba(63, 199, 182, 0.5);
          box-shadow: 0 0 0 3px rgba(63, 199, 182, 0.12);
        }
      `}</style>
    </div>
  );
}

function Field({
  label,
  required,
  hint,
  children,
}: {
  label: string;
  required?: boolean;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-ink">
        {label} {required && <span className="text-signal-rose">*</span>}
      </span>
      {children}
      {hint && <span className="text-[11px] text-ink-faint">{hint}</span>}
    </label>
  );
}
