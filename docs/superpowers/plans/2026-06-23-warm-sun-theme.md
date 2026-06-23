# 活力暖阳主题 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the "活力暖阳" warm color palette to the DeerFlow frontend by updating CSS variables in `globals.css`.

**Architecture:** Pure CSS variable replacement — modify the `:root` (light mode) and `.dark` (dark mode) selectors in `frontend/src/styles/globals.css`. All Shadcn UI components inherit colors through Tailwind's `@theme inline` mapping, so no component code changes are needed.

**Tech Stack:** CSS custom properties, oklch color space, Tailwind CSS v4

---

### Task 1: Update Light Mode CSS Variables

**Files:**
- Modify: `frontend/src/styles/globals.css:225-258` (`:root` selector block)

- [ ] **Step 1: Replace the entire `:root` selector block**

In `frontend/src/styles/globals.css`, replace the `:root` block (lines 225–258) with the warm palette values:

```css
:root {
  --radius: 0.625rem;
  --background: oklch(0.985 0.006 75);
  --foreground: oklch(0.145 0.005 50);
  --card: oklch(0.985 0.006 75);
  --card-foreground: oklch(0.145 0.005 50);
  --popover: oklch(0.985 0.006 75);
  --popover-foreground: oklch(0.145 0.005 50);
  --primary: oklch(0.67 0.18 50);
  --primary-foreground: oklch(0.99 0 0);
  --secondary: oklch(0.95 0.025 70);
  --secondary-foreground: oklch(0.35 0.06 50);
  --muted: oklch(0.97 0.012 70);
  --muted-foreground: oklch(0.55 0.01 50);
  --accent: oklch(0.95 0.025 70);
  --accent-foreground: oklch(0.35 0.06 50);
  --destructive: oklch(0.68 0.19 25);
  --border: oklch(0.93 0.012 70);
  --input: oklch(0.90 0.015 70);
  --ring: #F37F3E;
  --chart-1: oklch(0.67 0.18 50);
  --chart-2: oklch(0.68 0.19 25);
  --chart-3: oklch(0.87 0.17 95);
  --chart-4: oklch(0.78 0.12 55);
  --chart-5: oklch(0.62 0.17 22);
  --sidebar: oklch(0.985 0.006 75);
  --sidebar-foreground: oklch(0.145 0.005 50);
  --sidebar-primary: oklch(0.67 0.18 50);
  --sidebar-primary-foreground: oklch(0.99 0 0);
  --sidebar-accent: oklch(0.95 0.025 70);
  --sidebar-accent-foreground: oklch(0.35 0.06 50);
  --sidebar-border: oklch(0.93 0.012 70);
  --sidebar-ring: oklch(0.67 0.18 50);
}
```

- [ ] **Step 2: Verify light mode visually**

Run: `cd frontend && pnpm dev`

Open http://localhost:3000 in browser. Verify:
- Page background is warm white (not pure white)
- Primary buttons appear orange (not black)
- Input focus ring is orange
- Secondary buttons have peach/warm tone
- Overall feel is warm and inviting

- [ ] **Step 3: Commit light mode changes**

```bash
git add frontend/src/styles/globals.css
git commit -m "style: apply warm sun palette to light mode"
```

---

### Task 2: Update Dark Mode CSS Variables

**Files:**
- Modify: `frontend/src/styles/globals.css:260-293` (`.dark` selector block)

- [ ] **Step 1: Replace the entire `.dark` selector block**

In `frontend/src/styles/globals.css`, replace the `.dark` block (lines 260–293) with the warm dark palette:

```css
.dark {
  --background: oklch(0.18 0.01 50);
  --foreground: oklch(0.97 0.005 70);
  --card: oklch(0.20 0.012 50);
  --card-foreground: oklch(0.97 0.005 70);
  --popover: oklch(0.18 0.012 50);
  --popover-foreground: oklch(0.97 0.005 70);
  --primary: oklch(0.72 0.16 55);
  --primary-foreground: oklch(0.15 0.01 50);
  --secondary: oklch(0.27 0.02 50);
  --secondary-foreground: oklch(0.90 0.02 70);
  --muted: oklch(0.24 0.015 50);
  --muted-foreground: oklch(0.68 0.01 50);
  --accent: oklch(0.27 0.02 50);
  --accent-foreground: oklch(0.90 0.02 70);
  --destructive: oklch(0.72 0.18 25);
  --border: oklch(1 0.01 70 / 10%);
  --input: oklch(1 0.01 70 / 15%);
  --ring: oklch(0.72 0.16 55);
  --chart-1: oklch(0.72 0.16 55);
  --chart-2: oklch(0.72 0.17 25);
  --chart-3: oklch(0.85 0.15 95);
  --chart-4: oklch(0.75 0.10 55);
  --chart-5: oklch(0.68 0.15 22);
  --sidebar: oklch(0.18 0.012 50);
  --sidebar-foreground: oklch(0.97 0.005 70);
  --sidebar-primary: oklch(0.72 0.16 55);
  --sidebar-primary-foreground: oklch(0.99 0 0);
  --sidebar-accent: oklch(0.27 0.02 50);
  --sidebar-accent-foreground: oklch(0.90 0.02 70);
  --sidebar-border: oklch(1 0.01 70 / 10%);
  --sidebar-ring: oklch(0.72 0.16 55);
  font-weight: 300;
}
```

Note: `font-weight: 300` is preserved from the original.

- [ ] **Step 2: Verify dark mode visually**

With dev server running, switch to dark mode via the appearance settings or browser preference. Verify:
- Background is warm brown-black (not cold gray)
- Primary buttons appear warm orange (not white)
- Focus ring is visible orange (was transparent before)
- Text has warm undertone
- Overall feel is warm, not cold/clinical

- [ ] **Step 3: Commit dark mode changes**

```bash
git add frontend/src/styles/globals.css
git commit -m "style: apply warm sun palette to dark mode"
```

---

### Task 3: Final Verification

- [ ] **Step 1: Run lint and type check**

```bash
cd frontend && pnpm check
```

Expected: No errors (CSS changes don't affect lint or types, but confirm no regressions).

- [ ] **Step 2: Verify both modes side by side**

Open the app in browser. Check these pages/components in both light and dark mode:
- Landing page (forced dark mode)
- Chat workspace (main area)
- Settings dialog
- Sidebar navigation
- Input fields (focused and unfocused states)
- Buttons (primary, secondary, destructive variants)

- [ ] **Step 3: Final commit**

```bash
git add -A
git commit -m "style: complete warm sun theme application"
```

---

## Rollback

If any issues arise, revert with:

```bash
git checkout HEAD~2 -- frontend/src/styles/globals.css
```
