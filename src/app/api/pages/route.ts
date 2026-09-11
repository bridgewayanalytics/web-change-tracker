import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const PAGE_LABELS: Record<string, string> = {
  "naic.newsroom": "NAIC Newsroom",
  "naic.resource_center": "NAIC Resource Center",
  "naic.capital_markets_bureau": "Capital Markets Bureau",
  "naic.e.index": "E Committee Index",
  "naic.e.statutory_accounting_principles_wg": "Statutory Accounting Principles Working Group",
  "naic.e.blanks_wg": "Blanks Working Group",
  "naic.e.capital_adequacy_tf": "Capital Adequacy Task Force",
  "naic.e.health_rbc_wg": "Health Risk-Based Capital Working Group",
  "naic.e.life_rbc_wg": "Life Risk-Based Capital Working Group",
  "naic.ae.generator_economic_scenarios_sg": "Generator Economic Scenarios Subgroup",
  "naic.e.property_casualty_rbc_wg": "Property & Casualty Risk-Based Capital Working Group",
  "naic.e.rbc_investment_risk_evaluation_wg": "Risk-Based Capital Investment Risk Evaluation Working Group",
  "naic.e.receivership_insolvency_tf": "Receivership & Insolvency Task Force",
  "naic.e.financial_stability_tf": "Financial Stability Task Force",
  "naic.e.macroprudential_wg": "Macroprudential Working Group",
  "naic.e.group_capital_calculation_wg": "Group Capital Calculation Working Group",
  "naic.e.invested_assets_tf": "Invested Assets Task Force",
  "naic.e.credit_rating_provider_wg": "Credit Rating Provider Working Group",
  "naic.e.investment_analysis_wg": "Investment Analysis Working Group",
  "naic.e.investment_designation_analysis_wg": "Investment Designation Analysis Working Group",
  "naic.e.reinsurance.index": "Reinsurance (E) Index",
  "naic.e.reinsurance_financial_analysis_wg": "Reinsurance Financial Analysis Working Group",
  "naic.e.valuation_analysis_wg": "Valuation Analysis Working Group",
  "naic.ex.index": "EX Committee Index",
  "naic.ex.climate_resiliency_tf": "Climate Resiliency Task Force",
  "naic.ex.rbc_model_governance_tf": "RBC Model Governance Task Force",
  "naic.a.index": "A Committee Index",
  "naic.a.latf": "Life Actuarial Task Force",
  "naic.a.latf.vm22sg": "VM-22 Subgroup (LATF)",
};

export async function GET() {
  const pages = Object.entries(PAGE_LABELS)
    .map(([id, name]) => ({ id, name }))
    .sort((a, b) => a.name.localeCompare(b.name));
  return NextResponse.json(pages);
}
