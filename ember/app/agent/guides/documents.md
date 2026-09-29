MAKING DOCUMENTS (make_document)
Write the document as a .md file in your workspace (append long ones in parts), then call make_document with that
source and an output ending in .pdf. You get the PDF, an editable Word copy (.docx) and pictures of the first pages
next to it. The result says how many pages it has, where the text ends and what to fix; look at a page picture
to check the design. Change the source and remake until it is right.

SETTINGS: optional 'key: value' lines between two '---' lines at the very top (no comments after a value).
---
title: Weekly Planner
theme: modern          modern, classic, minimal or bold (each sets fonts, colours, heading and table styles)
page: A4               A4 or Letter; landscape: true for a wide page
font: sans             sans (looks like Calibri), serif (Cambria), display (Poppins); heading_font: the same
size: 10.5             text size in pt (7-16); line_height: 1.35 (1.0-2.2); margin: 18 (mm, 5-35)
accent: #2C3E50        colours as #RRGGBB: accent (headings, lines, table headers), text, muted, background
sidebar: left          left, right or none; sidebar_width: 62 (mm, 35-110); sidebar_background, sidebar_text
table: lines           lines, grid, zebra or plain
footer: Page {page} of {pages}
---

TEXT: # ## ### headings; **bold**, *italic*, `code`; [label](https://...) links; '- ' bullets; '1. ' numbers;
'- [ ] ' and '- [x] ' checkboxes; '> ' lines make a callout box; '---' alone is a divider line; end a line with
two spaces or \ for a line break. Tables: '| Day | Task |', then '|---|---:|' (:--- left, :---: centre, ---: right),
then rows; leave the first row's cells empty for a table without a header row.

LAYOUT LINES (each alone on its line):
::: sidebar  ...  ::: main     text for the sidebar (needs 'sidebar: left' or 'right'), then back to the main text
::: box #F4EFE6  ...  :::      a shaded box (the colour is optional)
::: columns 1:2  ::: column  ...  ::: column  ...  :::    2-4 columns side by side (widths optional)
::: center  ...  :::           centred text
::: photo 35x45 Your photo     a dashed box for a photo, width x height in mm (CV photos: 35x45)
::: lines 8                    writing lines to fill in
::: space 10                   empty space in mm
::: pagebreak                  start a new page
Boxes, columns and center can hold each other (4 deep); each one ends with its own ':::' line.

DESIGN THAT SELLS
- One accent colour, calm fonts, generous white space, a clear hierarchy: title, sections, details.
- Printables: one purpose per page; checklists, tables with empty rows and writing lines leave room to write.
- CV and letter templates: realistic sample text the buyer replaces, a sidebar for contact and skills, a photo box
  where photos are customary (Germany), and a matching cover letter in the same style.
- Offer what buyers search for: A4 and Letter versions (a document has one page size: make the Letter copy from a
  copy of the source with 'page: Letter'), and say which programs open the files (Word, Google Docs, Pages open
  .docx; any viewer prints the PDF). Describe only the sizes and files you made.
- Say in the product that it was made with AI help: on a notes page, or in a footer of printables buyers keep
  (planners, worksheets). Never in the footer of what buyers send or give to others (CVs, cover letters, letters,
  invitations): they would send your note with their application. The listing says it for those.
