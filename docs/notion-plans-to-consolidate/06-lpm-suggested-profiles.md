# lpm suggested profiles

> Local snapshot copied from Notion on 2026-07-28T11:22:19.800Z.  
> Source: [lpm suggested profiles](https://app.notion.com/p/3abc57f0654f809388f8eb708f0c7215)

\{  
"schema_version": "1.0",  
"composition_rule": "profile(niche, metro) = niche_base\[niche\] merged with metro_overlay\[metro\]; cells are computed, never hand-authored. missing_exact_profile must be unreachable.",  
"ternary_logic": \{  
"values": \["PASS", "FAIL", "UNKNOWN"\],  
"rule": "UNKNOWN never satisfies a requirement. UNKNOWN on a pain signal forbids that pain from appearing in outreach copy.",  
"render_failure": "render_status != ok forces all DOM-derived predicates to UNKNOWN"  
\},
"universal": \{  
"hard_disqualifiers": \[  
"business_status != OPERATIONAL",  
"on_suppression_list == true",  
"chain_footprint_score \>= 2",  
"location_count \>= 3",  
"rating \< niche.rating_floor",  
"review_count \> niche.review_ceiling",  
"gbp_primary_category in niche.excluded_categories",  
"money_terms_hit == 0 AND website_status == live",  
"reviews_trailing_365d == 0"  
\],  
"chain_footprint_signals": \{  
"location_selector_present": 2,  
"franchise_opportunities_link": 2,  
"domain_is_brand_path_city": 2,  
"independently_owned_disclaimer": 2,  
"gbp_name_matches_brand_of_city": 1,  
"footer_copyright_mismatch": 1,  
"corporate_800_number_alongside_local": 1,  
"threshold_fail": 2,  
"threshold_manual_review": 1  
\},  
"scale_window": \{  
"careers_open_roles_max": 4,  
"team_headshots_max": 8,  
"fleet_count_max": 6,  
"service_area_pages_max": 15,  
"named_marketing_role_present": "FAIL",  
"blog_cadence_2plus_per_month": "FAIL"  
\},  
"vendor_fingerprint": \{  
"premium_agency": \{ "verdict": "exclude", "points": 0, "examples": \["Scorpion", "Blue Corona", "Rival Digital", "Hook Agency", "Footbridge"\] \},  
"budget_reseller": \{ "verdict": "prioritize", "points": 10, "examples": \["Hibu", "Thryv", "Townsquare Interactive", "Yelp-built", "Vivial", "LocaliQ"\] \},  
"diy_builder": \{ "verdict": "prioritize", "points": 9, "examples": \["Wix", "GoDaddy Website Builder", "Squarespace default", "Google Sites"\] \},  
"social_only": \{ "verdict": "prioritize", "points": 10 \},  
"active_ppc": \{ "verdict": "qualify", "points": 7, "detect": \["CallRail", "CallTrackingMetrics", "Invoca", "gclid landing pages"\] \},  
"custom_modern": \{ "verdict": "exclude", "points": 0 \},  
"none_detected": \{ "verdict": "neutral", "points": 5 \}  
\},  
"pain_minimum_by_band": \{  
"some_to_established": 12,  
"established_to_mature": 20,  
"mature": 34,  
"mature_with_beatable_vendor": 30  
\}  
\},
"niche_base": \{  
"detailing": \{  
"label": "Auto detailing",  
"money_terms": \["ceramic coating", "graphene coating", "paint correction", "paint protection film", "ppf", "clear bra", "two-step correction", "one-step polish", "xpel", "suntek", "ceramic pro", "gtechniq", "modesta", "system x", "feynlab", "paint enhancement"\],  
"required_cooccurrence": \[\],  
"ticket_usd": \{ "low": 800, "high": 7000 \},  
"allowed_categories": \["car detailing service", "auto restoration service", "window tinting service"\],  
"excluded_categories": \["car wash", "self service car wash", "oil change service", "tire shop", "auto body shop"\],  
"excluded_patterns": \["express tunnel wash", "unlimited wash club", "touchless", "self-serve bay", "dealership recon only"\],  
"seed_chain_brands": \["Ziebart", "Tint World", "DetailXPerts", "Spiffy", "Zips", "Take 5"\],  
"rating_floor": 4.6,  
"pain_weights": \{  
"no_site_social_only": 40,  
"no_online_booking": 30,  
"instagram_active_site_dead": 28,  
"contact_dm_only": 25,  
"no_before_after_gallery": 22,  
"no_published_pricing": 20,  
"broken_or_absent_quote_form": 18,  
"no_mobile_tap_to_call": 16  
\},  
"offer_angle": "deposit-taking booking page with package selector",  
"buys": "booking"  
\},
```plain text
"epoxy": {
  "label": "Epoxy / concrete coatings",
  "money_terms": ["polyaspartic", "polyurea", "epoxy garage floor", "garage floor coating", "concrete coating", "flake floor", "full flake", "one day garage floor", "diamond grinding", "moisture test", "basement floor coating", "pool deck coating", "metallic epoxy", "chip system"],
  "required_cooccurrence": ["floor", "garage", "basement", "concrete", "slab", "patio"],
  "cooccurrence_note": "Required to suppress resin-art, river-table, countertop and marine-epoxy contamination in search results.",
  "ticket_usd": { "low": 2500, "high": 8000 },
  "allowed_categories": ["flooring contractor", "concrete contractor", "epoxy flooring contractor"],
  "excluded_categories": ["flooring store", "carpet store", "tile contractor", "general contractor"],
  "excluded_patterns": ["river table", "resin art", "countertop resin", "marine epoxy", "adhesive supplier"],
  "seed_chain_brands": ["Garage Force", "GarageExperts", "PremierGarage", "Garage Living", "Concrete Craft", "Slide-Lok"],
  "rating_floor": 4.5,
  "pain_weights": {
    "no_site_social_only": 38,
    "no_project_gallery_or_stock_only": 30,
    "no_quote_form": 26,
    "no_pricing_or_sqft_guidance": 22,
    "no_financing": 18,
    "no_warranty_terms": 14,
    "gallery_unverifiable_no_local_context": 12
  },
  "offer_angle": "photo-capture quote flow plus verified local gallery",
  "buys": "proof_and_quoting"
},

"garage_doors": {
  "label": "Garage doors",
  "money_terms": ["garage door installation", "new garage door", "door replacement", "clopay", "amarr", "c.h.i.", "wayne dalton", "haas", "raynor", "insulated door", "carriage house", "torsion spring", "broken spring", "opener installation", "liftmaster", "chamberlain", "same day service", "emergency garage door"],
  "required_cooccurrence": [],
  "ticket_usd": { "low": 200, "high": 4500 },
  "allowed_categories": ["garage door supplier", "garage door installer", "door repair service"],
  "excluded_categories": ["door supplier", "general contractor", "storage facility", "garage builder"],
  "excluded_patterns": ["commercial dock equipment only", "gate operators only"],
  "seed_chain_brands": ["Precision Door Service", "Overhead Door Company of", "1-800-GARAGE-DOOR"],
  "rating_floor": 4.4,
  "pain_weights": {
    "no_site_social_only": 36,
    "no_mobile_tap_to_call": 30,
    "no_same_day_emergency_language": 26,
    "no_online_scheduling": 22,
    "no_product_visualizer_or_gallery": 18,
    "phone_mismatch": 16,
    "no_service_area_pages": 14
  },
  "offer_angle": "mobile-first emergency repair page with two-tap booking",
  "buys": "speed_of_response"
},

"hardscaping": {
  "label": "Hardscaping (design-build)",
  "money_terms": ["paver patio", "retaining wall", "outdoor living", "outdoor kitchen", "fire pit", "fire feature", "pergola", "unilock", "techo-bloc", "belgard", "ep henry", "rosetta", "segmental retaining wall", "natural stone", "flagstone", "travertine", "pool deck", "3d design", "outdoor lighting"],
  "required_cooccurrence": [],
  "ticket_usd": { "low": 12000, "high": 120000 },
  "allowed_categories": ["landscaper", "landscape designer", "paving contractor", "masonry contractor"],
  "excluded_categories": ["lawn care service", "tree service", "snow removal service", "irrigation system supplier", "garden center"],
  "excluded_patterns": ["mowing only", "fertilization only", "commercial grounds maintenance", "snow only", "design only no install"],
  "seed_chain_brands": ["TruGreen", "Weed Man", "Lawn Doctor", "The Grounds Guys", "U.S. Lawns", "BrightView"],
  "rating_floor": 4.5,
  "pain_weights": {
    "no_site_social_only": 40,
    "no_project_portfolio": 34,
    "no_lead_form_at_high_ticket": 26,
    "no_design_process_explained": 24,
    "no_financing": 18,
    "no_booking_window_language": 16,
    "no_material_brand_affiliation": 14
  },
  "offer_angle": "portfolio-led consult request with budget-band prequalification",
  "buys": "credibility_at_price",
  "note": "Job frequency is 15-40/year. Review thresholds are structurally lower than volume trades."
},

"plumbing": {
  "label": "Plumbing",
  "money_terms": ["sewer line replacement", "trenchless", "pipe lining", "hydro jetting", "camera inspection", "sewer scope", "repipe", "water heater installation", "tankless", "sump pump", "battery backup sump", "backflow", "main water line", "excavation", "24/7 emergency"],
  "required_cooccurrence": [],
  "ticket_usd": { "low": 200, "high": 15000 },
  "allowed_categories": ["plumber", "drainage service", "water heater installer"],
  "excluded_categories": ["hvac contractor", "general contractor", "handyman", "plumbing supply store", "septic system service"],
  "excluded_patterns": ["new construction only", "multifamily only", "builders only", "commercial only"],
  "seed_chain_brands": ["Roto-Rooter", "Mr. Rooter", "Benjamin Franklin Plumbing", "Rescue Rooter", "ARS", "Zoom Drain", "bluefrog", "Len The Plumber"],
  "rating_floor": 4.4,
  "pain_weights": {
    "no_site_social_only": 34,
    "no_online_scheduling": 28,
    "no_emergency_after_hours_path": 26,
    "no_mobile_tap_to_call": 24,
    "broken_or_weak_form": 18,
    "no_service_area_pages": 18,
    "no_financing_on_large_jobs": 16,
    "no_membership_plan": 14
  },
  "offer_angle": "after-hours capture flow that books the 9pm emergency",
  "buys": "after_hours_capture"
},

"roofing": {
  "label": "Roofing",
  "money_terms": ["roof replacement", "tear off", "full roof", "gaf master elite", "owens corning preferred", "certainteed select", "storm damage", "hail damage", "insurance claim", "free inspection", "ice dam", "standing seam", "metal roof", "architectural shingle", "financing available"],
  "required_cooccurrence": [],
  "ticket_usd": { "low": 12000, "high": 60000 },
  "allowed_categories": ["roofing contractor", "siding contractor", "gutter service"],
  "excluded_categories": ["roofing supply store", "general contractor", "gutter cleaning service"],
  "excluded_patterns": ["commercial only", "tpo only", "storm chaser"],
  "storm_chaser_rule": {
    "logic": "AND",
    "conditions": ["gbp_created_within_months <= 12", "address_type in [virtual_office, mailbox_store, coworking]", "no_local_project_photos"],
    "verdict": "hard_fail"
  },
  "positive_signals": ["gaf master elite", "owens corning platinum", "certainteed select shingle master"],
  "positive_signal_note": "Manufacturer certification indicates ticket size and durability. It is NOT a franchise marker.",
  "seed_chain_brands": ["LeafFilter", "Leaf Home", "Erie Home", "West Shore Home", "Power Home Remodeling", "Renewal by Andersen"],
  "rating_floor": 4.4,
  "pain_weights": {
    "no_site_social_only": 36,
    "no_inspection_or_estimate_form": 30,
    "no_financing": 24,
    "no_insurance_claim_language": 22,
    "no_local_project_gallery": 22,
    "no_license_insurance_proof": 18,
    "no_storm_response_page": 14
  },
  "offer_angle": "free-inspection booking with address capture and photo upload",
  "buys": "inspection_volume"
},

"water_restoration": {
  "label": "Water damage restoration",
  "money_terms": ["water damage restoration", "flood cleanup", "basement flooding", "sewage cleanup", "mold remediation", "structural drying", "iicrc", "24/7 emergency response", "we bill your insurance", "direct insurance billing", "xactimate", "fire and smoke damage", "dehumidification", "moisture mapping", "emergency water extraction"],
  "required_cooccurrence": [],
  "ticket_usd": { "low": 3000, "high": 25000 },
  "allowed_categories": ["water damage restoration service", "fire damage restoration service", "waterproofing service"],
  "excluded_categories": ["carpet cleaning service", "general contractor", "building inspector", "plumber"],
  "excluded_patterns": ["mold testing only", "inspection only", "reconstruction only"],
  "seed_chain_brands": ["SERVPRO", "ServiceMaster Restore", "Paul Davis", "PuroClean", "Restoration 1", "911 Restoration", "Rainbow International", "BELFOR", "AdvantaClean", "Steamatic", "DKI", "Delta Restoration"],
  "rating_floor": 4.3,
  "pain_weights": {
    "no_site_social_only": 34,
    "no_24_7_language_above_fold": 32,
    "no_mobile_tap_to_call": 30,
    "no_insurance_billing_language": 26,
    "no_response_time_promise": 24,
    "form_is_primary_cta": 20,
    "no_certification_proof": 18,
    "no_emergency_vs_scheduled_split": 14
  },
  "offer_angle": "mobile emergency page built to convert panic traffic",
  "buys": "phone_call_capture",
  "inverted_signal_warning": "Generic 'missing estimate form = 20 points' is WRONG for this niche. A form-primary restoration site is the pain, not the absence of a form."
}
```
\},
"metro_overlay": \{  
"cle": \{  
"label": "Cleveland",  
"counties": \["Cuyahoga", "Lake", "Geauga", "Lorain", "Medina", "Summit", "Portage"\],  
"tier_a_suburbs": \["Beachwood", "Pepper Pike", "Hunting Valley", "Gates Mills", "Moreland Hills", "Chagrin Falls", "Shaker Heights", "Bratenahl", "Bay Village", "Rocky River", "Westlake", "Avon Lake", "Solon", "Hudson", "Bath", "Aurora", "Brecksville"\],  
"tier_b_suburbs": \["Strongsville", "Broadview Heights", "North Royalton", "Mentor", "Willoughby", "Medina", "Avon", "North Olmsted", "Twinsburg", "Parma Heights"\],  
"area_codes": \["216", "440", "330"\],  
"service_area_language": \["East Side", "West Side", "Chagrin Valley", "Northeast Ohio", "Greater Cleveland"\],  
"housing_stock": "older, deep basements, hillside lots, mature trees",  
"demand_modifiers": \{ "plumbing": 1.15, "water_restoration": 1.15, "hardscaping": 1.05, "roofing": 1.05, "epoxy": 0.9, "detailing": 1.0, "garage_doors": 1.0 \},  
"velocity_index": 1.0  
\},  
"cmh": \{  
"label": "Columbus",  
"counties": \["Franklin", "Delaware", "Union", "Licking", "Fairfield", "Madison", "Pickaway"\],  
"tier_a_suburbs": \["Dublin", "New Albany", "Powell", "Upper Arlington", "Bexley", "Worthington", "Granville", "Lewis Center", "Grandview Heights", "Muirfield"\],  
"tier_b_suburbs": \["Westerville", "Hilliard", "Gahanna", "Delaware", "Pickerington", "Canal Winchester", "Marysville", "Johnstown", "Sunbury"\],  
"area_codes": \["614", "380"\],  
"service_area_language": \["Central Ohio", "north side", "270 loop", "Greater Columbus"\],  
"housing_stock": "newer, 3-car attached garages, active new construction",  
"demand_modifiers": \{ "epoxy": 1.2, "hardscaping": 1.1, "detailing": 1.1, "garage_doors": 1.05, "plumbing": 0.95, "water_restoration": 0.95, "roofing": 1.0 \},  
"velocity_index": 1.22  
\}  
\},
"cells": \{  
"_confidence_note": "Rows marked measured match existing scoring_v3 config. Rows marked assumed are priors derived from ticket size, job frequency and metro velocity — replace with p25/p50/p90 of review_count computed from your own scraped corpus within 60 days.",  
"detailing__cle":          \{ "bands": \[10, 40, 125\], "pain_recency_days": 180, "confidence": "measured", "outreach_months": \[2,3,4,5,9,10\] \},  
"detailing__cmh":          \{ "bands": \[12, 50, 150\], "pain_recency_days": 180, "confidence": "measured", "outreach_months": \[2,3,4,5,9,10\] \},  
"plumbing__cle":           \{ "bands": \[5, 30, 100\],  "pain_recency_days": 150, "confidence": "measured", "outreach_months": \[9,10,11,2,3\] \},  
"water_restoration__cle":  \{ "bands": \[8, 30, 100\],  "pain_recency_days": 120, "confidence": "measured", "outreach_months": \[9,10,11,2,3\] \},  
"water_restoration__cmh":  \{ "bands": \[10, 35, 120\], "pain_recency_days": 120, "confidence": "measured", "outreach_months": \[9,10,11,2,3\] \},  
"epoxy__cle":              \{ "bands": \[6, 25, 80\],   "pain_recency_days": 240, "confidence": "assumed", "outreach_months": \[2,3,4,8,9,10\] \},  
"epoxy__cmh":              \{ "bands": \[8, 30, 100\],  "pain_recency_days": 240, "confidence": "assumed", "outreach_months": \[2,3,4,8,9,10\] \},  
"garage_doors__cle":       \{ "bands": \[10, 40, 150\], "pain_recency_days": 150, "confidence": "assumed", "outreach_months": \[8,9,10,3,4\] \},  
"garage_doors__cmh":       \{ "bands": \[12, 50, 175\], "pain_recency_days": 150, "confidence": "assumed", "outreach_months": \[8,9,10,3,4\] \},  
"hardscaping__cle":        \{ "bands": \[5, 20, 70\],   "pain_recency_days": 270, "confidence": "assumed", "outreach_months": \[12,1,2,7,8\] \},  
"hardscaping__cmh":        \{ "bands": \[6, 25, 85\],   "pain_recency_days": 270, "confidence": "assumed", "outreach_months": \[12,1,2,7,8\] \},  
"plumbing__cmh":           \{ "bands": \[6, 35, 120\],  "pain_recency_days": 150, "confidence": "assumed", "outreach_months": \[9,10,11,2,3\] \},  
"roofing__cle":            \{ "bands": \[8, 30, 110\],  "pain_recency_days": 180, "confidence": "assumed", "outreach_months": \[1,2,3\] \},  
"roofing__cmh":            \{ "bands": \[10, 40, 135\], "pain_recency_days": 180, "confidence": "assumed", "outreach_months": \[1,2,3\] \}  
\},
"review_ceilings": \{  
"detailing__cle": 400, "detailing__cmh": 450,  
"epoxy__cle": 300, "epoxy__cmh": 350,  
"garage_doors__cle": 800, "garage_doors__cmh": 900,  
"hardscaping__cle": 250, "hardscaping__cmh": 300,  
"plumbing__cle": 600, "plumbing__cmh": 700,  
"roofing__cle": 500, "roofing__cmh": 550,  
"water_restoration__cle": 400, "water_restoration__cmh": 450  
\},
"scoring_v3_weights": \{  
"icp_fit": 20,  
"demand_proof": 15,  
"review_velocity": 10,  
"problem_evidence": 25,  
"offer_fit": 15,  
"reachability": 10,  
"confidence": 5,  
"threshold": 70,  
"min_evidence_confidence": 3,  
"proposed_change": "Reallocate: move 10 points from icp_fit (now a binary gate, not a score) into a new vendor_fingerprint dimension. ICP fit should not be scored on a continuum after it has already passed a hard gate — that is double-counting."  
\}  
\}

