# Airtable Schema — Required Fields

| Field | Airtable Type | Exists | Notes |
|-------|--------------|--------|-------|
| Business Name | Single line text | ? |  |
| Owner Name | Single line text | ? |  |
| Vertical | Single select | ? | Options: detailing, epoxy, garage_doors, hardscaping, landscaping, pressure_washing, roofing, water_restoration |
| City | Single line text | ? |  |
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
| Review Demand Band | Single line text | ? |  |
| AI Confidence | Single select | ? | Options: low, medium, high |
| Score Total | Number | ? | Integer |
| Opener | Long text | ? |  |
| Recommended Action | Single line text | ? |  |
| Send Blockers | Long text | ? |  |
| Skip Reason | Single line text | ? |  |
| Place Key | Single line text | ? | Used as upsert key; must be unique per business |
| Status | Single select | ? | Options: New, Mockup Needed, Manual Review, Sent, Follow-Up, Closed |
| Outreach Channel | Single select | ? | Options: Google Maps, Email, Phone |
| Next Action | Single line text | ? |  |
| Last Contacted | Date | ? |  |
| Next Follow-Up | Date | ? |  |
| Assigned To | Single line text | ? |  |
| Mockup URL | URL | ? |  |
| Video URL | URL | ? |  |
| Packet Path | Single line text | ? | Local file path to the packet markdown file |
