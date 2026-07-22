# Airtable Schema — Required Fields

| Field | Airtable Type | Exists | Notes |
|-------|--------------|--------|-------|
| Business Name | Single line text | ? |  |
| Owner Name | Single line text | ? |  |
| Vertical | Single select | ? | Options: detailing, epoxy, garage_doors, hardscaping, landscaping, pressure_washing, roofing, water_restoration |
| City | Single line text | ? |  |
| Metro Context | Single line text | ? | Authoritative Operator metro-context slug |
| Phone | Phone number | ? |  |
| Email | Email | ? |  |
| Website URL | URL | ? |  |
| Google Maps URL | URL | ? |  |
| Reviews | Number | ? | Integer; set field to 'Integer' precision |
| Rating | Number | ? | Decimal; set field to 2 decimal places |
| Current Website Quality | Single line text | ? |  |
| Best Service Angle | Single select | ? | Options: No-Site Launch, Website Rebuild, Estimate Funnel, Automation / Follow-Up |
| Specific Observation | Long text | ? |  |
| Packet Eligible | Single select | ? | Options: Yes, No |
| Sendable | Single select | ? | Options: Yes, No |
| Lead Priority | Single select | ? | Options: A, B, C (grade) |
| Offer Fit | Single select | ? | Options: No-Site Launch, Website Rebuild, Estimate Funnel, Automation / Follow-Up |
| Site Status | Single select | ? | Options: Missing, Broken, No Website, Loaded, Redirect Issue, Social Only, Unknown |
| Contact Form Detected | Single select | ? | Options: Yes, No |
| Estimate Form Detected | Single select | ? | Options: Yes, No |
| Form Strength | Single line text | ? |  |
| Form Detection Method | Single line text | ? |  |
| CTA Strength | Single line text | ? |  |
| Visible Pain Score | Number | ? | Integer 0–5 |
| Visible Pain Reasons | Long text | ? |  |
| Send Trigger | Single line text | ? |  |
| Money Signal | Single line text | ? |  |
| Buyer Intent Signals | Long text | ? | Comma-separated signals detected by LLM |
| Commercial Fit State | Single select | ? | Options: commercially-sendable, minor-fix/nurture, needs-broader-system-evidence, intentional-or-ambiguous, not-an-LPM-fit, unverified |
| Commercial Materiality | Long text | ? | Evidence-backed commercial consequence |
| Recurring Consequence | Long text | ? | Why the issue repeats or creates downstream loss |
| Root Cause Confidence | Long text | ? | Bounded connection between evidence and mechanism |
| LPM Offer Component | Single line text | ? | One authoritative offer component |
| Packageable Solution | Long text | ? | Bounded deliverable LPM can build |
| Value Mechanism | Long text | ? | Credible gain or avoided-loss mechanism |
| Beyond Free Fix | Long text | ? | Why this is broader than one free edit |
| Self Fix Risk | Long text | ? | Likelihood and scope of self-resolution |
| Intentionality | Long text | ? | Why the condition is not intentional or ambiguous |
| Contact Eligibility | Single select | ? | Options: contact-now, contact-after-asset, hold-commercial-case |
| Asset State | Single select | ? | Options: none, planned, verified-existing |
| Promise State | Single select | ? | Options: asset-free, future-build, existing-verified-asset |
| Asset ID | Single line text | ? | Verified asset identifier when present |
| Asset Verifier Verdict | Single select | ? | Options: PASS, FAIL, BLOCK |
| Evidence Refs | Long text | ? | Structured evidence identifiers |
| Safe Outreach Claims | Long text | ? | Claims bound to evidence identifiers |
| Evidence Snapshot | Long text | ? | Structured bounded evidence used by the morning verifier |
| Review Demand Band | Single line text | ? |  |
| AI Confidence | Single select | ? | Options: low, medium, high |
| Score Total | Number | ? | Integer |
| Opener | Long text | ? |  |
| Recommended Action | Single line text | ? |  |
| Send Blockers | Long text | ? |  |
| Skip Reason | Single line text | ? |  |
| Place Key | Single line text | ? | Used as upsert key; must be unique per business |
| Status | Single select | ? | Options: New, Mockup Needed, Manual Review, Sent, Follow-Up, Closed |
| Outreach Channel | Single select | ? | Options: Email, Phone, Instagram DM, SMS |
| Consent Posture | Single line text | ? | Required and documented before any SMS draft can pass |
| Next Action | Single line text | ? |  |
| Last Contacted | Date | ? |  |
| Next Follow-Up | Date | ? |  |
| Assigned To | Single line text | ? |  |
| Mockup URL | URL | ? |  |
| Video URL | URL | ? |  |
| Packet Path | Single line text | ? | Local file path to the packet markdown file |
