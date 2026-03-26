# 🔬 Research Report: Mathematically Quantifying Voter Opinions Across Policy Topics
**Prepared for: Digital Democracy Project | March 6, 2026**

---

## EXECUTIVE SUMMARY

There is a robust and growing body of peer-reviewed scientific research that can mathematically quantify voter opinions across policy topics, identify clusters of like-minded citizens, reveal where alignment exists across groups, and produce results that are scientifically rigorous and publishable. This report organizes the best available methodologies into a practical framework DDP can use — from survey design through analysis and reporting.

---

## PART 1: THE CORE METHODOLOGICAL TOOLKIT

There are **five primary scientific methods** used to quantify voter opinions and identify policy position clusters. Each has distinct strengths and is often used in combination.

---

### 🔵 METHOD 1: K-Means Clustering
**Best for: Identifying "buckets" of policy positions from the bottom up**

**What it is:** A machine learning algorithm that partitions survey respondents into *k* groups based on their positions across multiple policy issues simultaneously. Unlike party-based analysis, it finds clusters organically from the data — no assumptions about who belongs where.

**The Gold Standard Paper (2026):**
> Young, D.J., Ackland, J., Kapounek, A., Madsen, J.K., Greening, L.J., & de-Wit, L. (2026). *"A new measure of issue polarization using k-means clustering: US trends 1988–2024 and predictors of polarization across the world."* **Royal Society Open Science**, 13(2): 251428. DOI: 10.1098/rsos.251428

This is the most current and rigorous application of k-means to voter opinion data. Published February 2026 in a peer-reviewed open-access journal, it uses **American National Election Studies (ANES)** data across 36 years and 114 countries.

**How it works — the three-metric framework:**
The paper introduces three mathematically precise measures of how polarized or aligned opinion clusters are:

| Metric | What It Measures | Formula | Interpretation |
|--------|-----------------|---------|----------------|
| **Separation** | Distance between cluster centers across all issues | Mean absolute difference between cluster means per issue, averaged across issues | Higher = more polarized / more distinct buckets |
| **Dispersion** | Internal cohesion within each cluster | Mean absolute residual of each person from their cluster center | Lower = tighter, more coherent groups |
| **Equality-of-Size** | Whether clusters are balanced in size | Shannon entropy: P₁·log(1/P₁) + P₂·log(1/P₂) | Higher = more evenly split population |

**Key findings relevant to DDP:**
- US polarization increased 64% from 1988–2024 (Separation score: 0.22 → 0.36)
- The increase was driven almost entirely by **cultural issues**, not economic ones
- Globally, lower-HDI countries have large conservative clusters; higher-HDI countries have more equally-sized liberal/conservative clusters
- **Open-source R code available:** https://osf.io/kzd23/

**Why it's ideal for DDP:** You can apply this exact methodology to your own survey data on RCV, open primaries, and mobile voting to identify which "buckets" of voters exist and how far apart they are.

---

### 🟢 METHOD 2: Gaussian Mixture Model (GMM) Clustering
**Best for: Allowing unequal cluster sizes; finding the *optimal* number of clusters**

**What it is:** A probabilistic extension of k-means that fits multivariate Gaussian distributions to the data. Unlike k-means (which forces equal-sized clusters), GMM can find a large centrist cluster and a small extremist cluster simultaneously. Uses the **Akaike Information Criterion (AIC)** to determine the statistically optimal number of clusters.

**Key Paper:**
> Lee, L., Zhang, S., & Yang, V.C. (Northwestern University). *"Do two parties represent the US? Clustering analysis of US public ideology survey."* SIAM Undergraduate Research Online. Available: https://www.siam.org/media/v2coalpr/s01651.pdf

**Key findings:**
- When forced into 2 clusters: found a **large centrist cluster (73%)** and a **small right-wing cluster (27%)**
- The Democratic Party position aligned with the centrist cluster; the Republican Party sat *between* the two clusters
- When the number of clusters was freed: **3 clusters** provided the best statistical fit (left, center, right)
- The 4-cluster analysis revealed that the extra clusters were **not** simply party sub-segments — they were genuinely cross-partisan ideological groups

**Mathematical formula:**
$$p(x|\mu_1...\mu_m, \Sigma_1...\Sigma_m) = \sum_{i=1}^{m} \alpha_i N_i(x|\mu_i, \Sigma_i)$$

Where *m* = number of clusters, *α* = cluster weight, *μ* = cluster center, *Σ* = covariance matrix.

**Why it's ideal for DDP:** GMM is better than k-means when you expect unequal group sizes (e.g., a large "persuadable middle" on open primaries). The AIC model selection gives you a scientifically defensible answer to "how many distinct voter types exist?"

---

### 🟡 METHOD 3: Principal Component Analysis (PCA) + Dimensionality Reduction
**Best for: Mapping the underlying ideological dimensions driving voter opinion**

**What it is:** PCA reduces a large number of policy questions down to a small number of **latent ideological dimensions** (e.g., economic left-right, cultural liberal-conservative). This reveals the hidden structure of voter opinion and is typically used *before* clustering to improve cluster quality.

**How it works in practice:**
1. Survey respondents answer 10–20 policy questions on Likert scales
2. PCA identifies which questions "load" together (i.e., which issues are correlated)
3. The first 2–3 principal components explain most of the variance
4. Respondents are plotted in this reduced space, revealing natural groupings

**Key finding from the Cambridge/LSE paper (Young et al. 2026):**
- PCA reduced 14 ANES policy items to 3 dimensions (social, economic, racial equality)
- Clustering on PCA-reduced data produced results with *r* ≥ 0.995 correlation to full-data clustering — meaning PCA doesn't distort the results but dramatically improves computational efficiency

**Why it's ideal for DDP:** If you survey voters on 15+ policy topics (RCV, open primaries, mobile voting, campaign finance, redistricting, etc.), PCA will tell you which issues cluster together in voters' minds — revealing the underlying ideological structure of your issue space.

---

### 🟠 METHOD 4: Conjoint Survey Experiments
**Best for: Measuring the *relative weight* voters place on different policy positions**

**What it is:** Respondents are shown pairs of hypothetical candidates or policy packages and asked to choose between them. By varying the attributes systematically, researchers can calculate the **Average Marginal Component Effect (AMCE)** — the causal effect of each policy position on voter preference.

**Key Paper:**
> Hainmueller, J., Hopkins, D.J., & Yamamoto, T. (2014). *"Causal Inference in Conjoint Analysis."* **Political Analysis**, 22(1): 1-30.

**Application to RCV/Open Primaries:**
> Boatright, R., Tolbert, C. & Micatka, N.K. (2024). *"Public Opinion on Reforming U.S. Primaries."* **Social Science Quarterly**, 105(3): 876-893.
- Found **58% of Americans favor RCV for primaries** among those expressing an opinion
- Strong partisan differences: Republicans significantly less favorable to election reform

**Why it's ideal for DDP:** Conjoint experiments can answer: *"How much does support for open primaries change when voters learn it would include independents? How much does support for RCV change when framed as 'majority winners' vs. 'ranked ballots'?"* This is the gold standard for causal inference in survey research.

---

### 🔴 METHOD 5: Ideal Point Estimation (IRT / DW-NOMINATE)
**Best for: Placing voters AND legislators on the same ideological scale**

**What it is:** Item Response Theory (IRT) models treat policy questions like test items and estimate each respondent's "ideal point" on a latent ideological dimension. DW-NOMINATE is the legislative version, placing legislators on a 1D or 2D ideological map based on roll-call votes.

**Key application for DDP:**
- You can estimate ideal points for both **voters** (from survey responses) and **legislators** (from voting records)
- This allows direct comparison: *"How far is this legislator's ideal point from the median voter in their district?"*
- Directly relevant to DDP's legislator scorecard work

**Key resource:** voteview.com (free, open-access DW-NOMINATE scores for all members of Congress)

---

## PART 2: THE BEST EXISTING DATASETS

These are the primary open-access datasets used in peer-reviewed research — all free to use:

| Dataset | Organization | Sample Size | Coverage | Best For |
|---------|-------------|-------------|----------|----------|
| **American National Election Studies (ANES)** | Stanford/U. Michigan | ~5,000–30,000/wave | 1948–2024, national | Longitudinal voter opinion, policy positions |
| **Cooperative Election Study (CES)** | Harvard/YouGov | 50,000+/year | 2006–2024, national | Large-N, state-level breakdowns, RCV questions |
| **World Values Survey (WVS)** | WVS Association | ~1,000–3,000/country | 57+ countries | Cross-national comparison |
| **Pew Political Typology Survey** | Pew Research Center | ~10,000 | 1987–2021 | Pre-built 9-cluster typology |
| **Voter Study Group (VOTER Survey)** | Democracy Fund | ~8,000 | 2011–present | Longitudinal panel, independent voters |

**Most relevant for DDP's work:**
- **CES 2024** includes questions on RCV awareness and support (67% of Americans had heard of RCV by 2024, up from 56% in 2022)
- **ANES** has the longest time series and is used in the most peer-reviewed studies
- **Voter Study Group** specifically tracks independent/NPA voters — directly relevant to DDP's open primaries work

---

## PART 3: THE PEW POLITICAL TYPOLOGY — A READY-MADE FRAMEWORK

Pew Research Center has been conducting cluster-based voter typology studies since 1987. Their **2021 typology** used cluster analysis to identify **9 distinct voter segments** based on values, policy positions, and political behavior:

| Segment | Size | Key Characteristics |
|---------|------|---------------------|
| Faith & Flag Conservatives | 10% | Most conservative; strong religious identity |
| Committed Conservatives | 7% | Traditional Republican; free market, limited govt |
| Populist Right | 11% | Anti-establishment; economically populist |
| Ambivalent Right | 12% | Moderate; socially liberal but fiscally conservative |
| **Stressed Sideliners** | **15%** | **Least engaged; economically anxious; persuadable** |
| Outsider Left | 10% | Progressive but anti-establishment |
| Democratic Mainstays | 16% | Older, moderate Democrats; backbone of party |
| Establishment Liberals | 13% | College-educated; pro-institution |
| Progressive Left | 6% | Most liberal; highly educated; activist |

**Why this matters for DDP:** The **Stressed Sideliners** (15% of the electorate) are the largest single persuadable group — disengaged, economically anxious, and not strongly partisan. This is the exact population that open primaries and mobile voting reforms are designed to re-engage.

---

## PART 4: KEY FINDINGS FROM THE LITERATURE DIRECTLY RELEVANT TO DDP

### On Ranked Choice Voting:
- **86%** of registered voters in RCV-ballot states said it is "very or somewhat important" that the winning candidate has a majority of votes (YouGov 2024)
- **67%** of Americans had heard of RCV by 2024 (up from 56% in 2022) — CES data
- **75–78%** of voters ages 18–29 support RCV across two national surveys (Dowling 2025)
- Support drops with age: only **18–40%** of voters 70+ favor RCV
- Latino, Asian American, and MENA respondents more inclined to support RCV than white respondents (Anthony et al. 2024)
- RCV is associated with **increased voter turnout** across both high and low SES groups (Dowling et al. 2024)

### On Open Primaries / Independent Voters:
- **53%** of likely US voters believe neither party represents the American people (Rasmussen)
- GMM clustering finds the US public is better represented by **3 clusters** (left, center, right) than 2 parties
- The large centrist cluster (58–76% of the population) is poorly served by the current two-party primary system
- Independents are disproportionately represented in the centrist cluster

### On Polarization:
- US issue polarization increased 64% from 1988–2024, driven primarily by **cultural issues** (Young et al. 2026)
- The increase was concentrated in the period **2008–2020**
- Both clusters have moved — the left cluster moved left, the right cluster moved slightly right
- **Sorting** (people aligning their party ID with their actual views) has increased over time

---

## PART 5: RECOMMENDED METHODOLOGY FOR DDP

Based on this research, here is a scientifically rigorous, step-by-step approach DDP could implement:

### Step 1: Survey Design
- Use **Likert-scale questions** (5 or 7 points) on 15–25 policy topics
- Include DDP's core issues: RCV, open primaries, mobile voting, campaign finance, redistricting, plus "bridge" issues (healthcare, economy, immigration) to anchor respondents in the broader ideological space
- Include demographic variables: age, race, party ID, education, income, geography
- Target **n ≥ 1,000** for statistical power; **n ≥ 3,000** for subgroup analysis
- Partner with YouGov, Lucid, or Qualtrics for nationally representative sampling

### Step 2: Dimensionality Reduction (PCA)
- Z-score all variables
- Run PCA to identify 2–4 latent ideological dimensions
- Retain components explaining ≥ 70% of variance
- This reveals: *"What are the underlying ideological axes that structure voter opinion on these issues?"*

### Step 3: Cluster Analysis (GMM + k-means)
- Run **Gaussian Mixture Model** with AIC model selection to find the optimal number of clusters (likely 3–5)
- Validate with k-means for robustness
- Calculate **Separation, Dispersion, and Equality-of-Size** (Young et al. 2026 framework)
- Profile each cluster: mean positions on each issue, demographic composition, party ID

### Step 4: Alignment Analysis
- Calculate **inter-cluster agreement** on each policy issue
- Identify issues where clusters converge (high alignment = bipartisan opportunity)
- Identify issues where clusters diverge (high separation = polarized terrain)
- For DDP: this will show which election reform proposals have cross-cluster support

### Step 5: Reporting
- **Cluster profiles:** Named, described voter segments (like Pew typology)
- **Issue alignment matrix:** Heatmap showing agreement/disagreement across clusters on each policy
- **Separation scores:** Quantified polarization per issue (0–1 scale)
- **Demographic breakdowns:** Who is in each cluster by age, race, party, geography
- **Longitudinal tracking:** Repeat annually to track movement

---

## PART 6: KEY SOURCES & CITATIONS

| Source | Type | Access | Relevance |
|--------|------|--------|-----------|
| Young et al. (2026). *k-means clustering & issue polarization.* Royal Society Open Science. DOI: 10.1098/rsos.251428 | Peer-reviewed | **Open Access** | Primary methodology |
| Lee, Zhang & Yang. *GMM clustering of US public ideology.* SIAM. siam.org/media/v2coalpr/s01651.pdf | Academic paper | **Free PDF** | GMM methodology |
| Dowling & Tolbert (2025). *What We Know About RCV.* ABA Task Force. americanbar.org | Policy review | **Free** | RCV public opinion synthesis |
| Boatright, Tolbert & Micatka (2024). *Public Opinion on Reforming US Primaries.* Social Science Quarterly, 105(3). | Peer-reviewed | Subscription | Open primaries opinion data |
| Pew Research Center (2021). *Beyond Red vs. Blue: The Political Typology.* pewresearch.org | Research report | **Free** | Ready-made 9-cluster typology |
| ANES Time Series Data (1948–2024) | Dataset | **Free** | electionstudies.org |
| Cooperative Election Study (2006–2024) | Dataset | **Free** | cces.gov.harvard.edu |
| Democracy Fund Voter Study Group | Dataset + reports | **Free** | voterstudygroup.org |
| OSF Replication Code (Young et al.) | R code | **Free** | osf.io/kzd23 |

---

## BOTTOM LINE FOR DDP

The science is mature, the tools are free, and the methodology is well-established. The most practical path forward for DDP would be to:

1. **Commission a custom survey** (n=2,000–3,000) using the ANES/CES question battery as a backbone, adding DDP-specific questions on RCV, open primaries, and mobile voting
2. **Apply GMM + PCA clustering** (using the open-source R code from Young et al. 2026) to identify 3–5 voter segments
3. **Report results** using the Separation/Dispersion/Equality-of-Size framework — giving you a scientifically defensible, quantified picture of where voter opinion clusters exist and where cross-partisan alignment is possible
4. **Repeat annually** to track movement over time — building a longitudinal dataset that becomes increasingly valuable

This approach would produce results that are **publishable in peer-reviewed journals**, credible to policymakers, and directly actionable for DDP's advocacy work.