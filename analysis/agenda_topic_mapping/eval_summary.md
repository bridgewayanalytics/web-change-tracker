# Historical Evaluation Summary

**Total resources evaluated:** 100

## Topic Suggestion

| Metric | Value |
|--------|-------|
| Resources with actual topic | 100 |
| Resources where system predicted a topic | 96 |
| Correct predictions | 86 |
| Accuracy (correct / has_actual) | 86.0% |
| Coverage (predicted / has_actual) | 96.0% |

### Topic source breakdown

| Source | Count |
|--------|-------|
| agenda_item_inheritance | 66 |
| ai_classification | 30 |
| unresolved | 4 |

## Agenda Item Matching

| Metric | Value |
|--------|-------|
| Resources with actual agenda item link | 55 |
| Resources where system predicted agenda items | 68 |
| Correct matches (overlap with actual) | 55 |
| Match rate | 100.0% |

## Sample Results

### Correct topic predictions

- **Climate and Resiliency (EX) Task Force - November 19, 2024 - Meeting Agenda**
  - Actual: [b]NAIC Climate Initiatives[/b]
  - Predicted: NAIC Climate Initiatives (via ai_classification)
- **The Capital Adequacy (E) Task Force November 18, 2024, 3:00 PM ET Meeting Agenda**
  - Actual: [b]Collateral Loans[/b]
  - Predicted: Collateral Loans (via agenda_item_inheritance)
- **International Insurance Relations (G) Committee November 17, 2024, Agenda**
  - Actual: [b]International Association of Insurance Supervisors (IAIS)[/b]
  - Predicted: International Association of Insurance Supervisors (IAIS) (via agenda_item_inheritance)
- **Meeting of Executive (EX) Committee November 19, 2024, Agenda**
  - Actual: [b]The NAIC Investment Oversight Framework[/b]
  - Predicted: The NAIC Investment Oversight Framework (via agenda_item_inheritance)
- **Achieving Consistent and Comparable Climate-related Disclosures: 2024 Progress**
  - Actual: [b][color=rgb(34, 34, 34)]Financial Stability Board (FSB)[/color][/b]
  - Predicted: Financial Stability Board (FSB) (via ai_classification)
- **ISSA 5000 General Requirements for Sustainability Assurance Engagements - Final **
  - Actual: [b]International Standard on Sustainability Assurance (ISSA-5000)[/b]
  - Predicted: International Standard on Sustainability Assurance (ISSA-5000) (via ai_classification)
- **ISSA 5000 General Requirements for Sustainability Assurance Engagements - Basis **
  - Actual: [b]International Standard on Sustainability Assurance (ISSA-5000)[/b]
  - Predicted: International Standard on Sustainability Assurance (ISSA-5000) (via ai_classification)
- **FAQ on the Planned Adoption and Implementation of the ICS and Conclusion of the **
  - Actual: [b]International Association of Insurance Supervisors (IAIS)[/b]
  - Predicted: International Association of Insurance Supervisors (IAIS) (via agenda_item_inheritance)
- **Report on Aggregation Method Comparability Assessment**
  - Actual: [b]International Association of Insurance Supervisors (IAIS)[/b]
  - Predicted: International Association of Insurance Supervisors (IAIS) (via agenda_item_inheritance)
- **Report on Aggregation Method Comparability Assessment**
  - Actual: [b]International Association of Insurance Supervisors (IAIS)[/b]
  - Predicted: International Association of Insurance Supervisors (IAIS) (via agenda_item_inheritance)

### Incorrect topic predictions

- **Achieving Consistent and Comparable Climate-related Disclosures: 2024 Progress**
  - Actual: [b]International Accounting Standards Board (IASB)[/b]
  - Predicted: Financial Stability Board (FSB) (via ai_classification)
- **U.S. Insurer Investments in Private-Label Commercial Mortgage-Backed Securities **
  - Actual: [b]Residential Mortgage Funds Under Schedule BA[/b]
  - Predicted: CMBS & RMBS (via ai_classification)
- **Evolution of Asset Intensive Insurance Report**
  - Actual: [b]Credit for Reinsurance[/b]
  - Predicted: The Bermuda Monetary Authority (BMA) (via ai_classification)
- **Agenda & Materials RBC-IRE-WG - February 11, 2024**
  - Actual: [b]Collateralized Loan Obligations (CLOs) and Asset-Backed Securities (ABS)[/b]
  - Predicted: Exchange Traded Funds (ETFs) on the SVO-Identified Bond List (via agenda_item_inheritance)
- **The ACLI RBC Principles for Bond Funds Presentation**
  - Actual: [b]Funds Under Schedule BA[/b]
  - Predicted: Exchange Traded Funds (ETFs) on the SVO-Identified Bond List (via agenda_item_inheritance)
- **SAPWG Meeting Agenda & Materials - February 25, 2025**
  - Actual: ALM Derivatives & Derivative Investments
  - Predicted: Tax Credit Structures (via agenda_item_inheritance)
- **Accounting Practices & Procedures Manual - March 2025**
  - Actual: [b]Principles-Based Bond Definition and Reporting[/b]
  - Predicted: Tax Credit Structures (via agenda_item_inheritance)
- **LATF Meeting Agenda & Materials - March 22 & 23, 2025**
  - Actual: [b]Credit for Reinsurance[/b]
  - Predicted: Generator of Economic Scenarios (GOES) (via agenda_item_inheritance)
- **AMI-WG Meeting Agenda - March 25, 2025**
  - Actual: [b]Group Capital Calculations[/b]
  - Predicted: International Association of Insurance Supervisors (IAIS) (via ai_classification)
- **Life Insurers’ Role in the Intermediation Chain of Public and Private Credit to **
  - Actual: [b]Collateralized Loan Obligations (CLOs) and Asset-Backed Securities (ABS)[/b]
  - Predicted: The Federal Reserve Board (FRB) (via ai_classification)

### Unresolved (4 resources with actual topic but no prediction)

- **SAPWG Adoptions (updated 11/17/2024)** (actual: [b]Principles-Based Bond Definition and Reporting[/b])
- **NAIC 2025 Spring National Meeting Tentative Agenda** (actual: Calendar Events with no Topic)
- **RBC-MG-TF Meeting Agenda & Materials - March 25, 2025** (actual: [b]The NAIC Investment Oversight Framework[/b])
- **Insurers Cited in 777-Related Scheme** (actual: [b]Principles-Based Bond Definition and Reporting[/b])

---

*Generated by `analysis/agenda_topic_mapping/eval_historical.py`*