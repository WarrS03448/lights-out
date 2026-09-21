# Lights Out project instructions

## UI design and refresh requirements (Sam, 2026-09-21)

- Every UI addition or change must follow the established Lights Out design in the client, website, and admin console, without exception. Reuse the existing components, colors, typography, spacing, borders, and interaction patterns. Do not introduce a new visual style for a feature.
- Use sharp, square corners (`border-radius: 0`) for UI panels, cards, buttons, menus, inputs, badges, and message bubbles. Do not add rounded corners. Match the surrounding established UI when extending it.
- Design every client UI element for repeated live snapshots and background refreshes. A refresh must preserve an open dropdown or menu, keyboard focus and caret/selection, unsent drafts, scroll position, expanded sections, and the selected conversation. Do not detach or replace an active native control to update unrelated data. Keep live status, counts, timers, and messages current without interrupting the user's interaction.
- Scope preserved state to the signed-in account and relevant event/conversation. Clear it immediately on sign-out or account changes so private state cannot cross accounts. Explicit navigation and completed user actions may reset the state they intentionally change.
- Verify interactive UI under several consecutive changed snapshots, including unrelated status/timer changes and relevant data updates while the user is interacting. Check keyboard and pointer use, account switching, and supported window sizes/languages. Verify a refresh bug with a regression test that reproduces the interruption; an unchanged snapshot is not a refresh test.
