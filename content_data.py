"""
content_data.py
All static seed data for explainers, research articles, and blog posts.
Imported by routers/content.py — single source of truth.

v3 changes:
  - Added "author", "context", "technicalDetail", "impact" fields to every EXPLAINERS_SEED entry
  - Added BLOG_POSTS_SEED list matching the BlogPost interface in blog.ts
  Images are served from /images/... via FastAPI StaticFiles mount.
"""

EXPLAINERS_SEED = [
    {
        "id": "quantum-dog",
        "title": "The Quantum Dog: Schrödinger's Pet Paradox",
        "subtitle": "How quantum superposition works — explained through a thought experiment about a very confused dog.",
        "field": "QUANTUM PHYSICS",
        "badgeColor": "cyan",
        "readTime": "8 MIN READ",
        "author": "Dr. Elena Voss",
        "image": "/images/explainers/quantum-dog.jpg",
        "content": [
            "Imagine a dog inside a sealed kennel. According to quantum mechanics, until you open the door and observe the dog, it exists in a superposition of all possible states — sleeping, playing, barking, and eating simultaneously.",
            "This thought experiment, inspired by Schrödinger's famous cat paradox, illustrates one of the most counterintuitive aspects of quantum mechanics: superposition. In the quantum world, particles don't have definite properties until they're measured.",
            "The act of measurement \"collapses\" the wave function, forcing the system to choose one definite state. Before measurement, all possibilities coexist in a mathematical framework called the wave function.",
            "Quantum decoherence explains why we don't see dogs in superposition in real life. The environment constantly \"measures\" macroscopic objects, collapsing their quantum states almost instantaneously.",
            "Modern quantum computers exploit superposition by using qubits that can be 0 and 1 simultaneously, enabling parallel computation on an exponential scale.",
            "The implications extend beyond computing: quantum sensing, quantum cryptography, and quantum networks all leverage these strange properties of nature.",
        ],
        "keyInsights": [
            "Superposition allows particles to exist in multiple states simultaneously",
            "Measurement collapses the wave function to a definite state",
            "Quantum decoherence prevents macroscopic superposition",
            "Quantum computers leverage superposition for exponential speedup",
        ],
        "context": "Schrödinger's thought experiment was first proposed in 1935 as a critique of the Copenhagen interpretation of quantum mechanics. It was designed to highlight the absurdity of applying quantum rules to everyday objects — yet decades later, experiments have confirmed superposition at increasingly large scales, from molecules to vibrating drumheads.",
        "technicalDetail": "The wave function Ψ encodes all possible measurement outcomes as probability amplitudes. When a measurement is made, the system \"collapses\" into one eigenstate with probability |⟨ψ|ϕ⟩|². Decoherence timescales for macroscopic objects are on the order of 10⁻³⁹ seconds, making real-world superposition of large objects effectively impossible without extreme isolation.",
        "impact": "Quantum superposition underpins the entire quantum computing revolution. Companies like IBM, Google, and startups such as IonQ are racing to build fault-tolerant quantum computers that could transform drug discovery, cryptography, and materials science within the next decade.",
    },
    {
        "id": "crispr-scissors",
        "title": "CRISPR: The Molecular Scissors Rewriting Life",
        "subtitle": "Gene editing technology that could cure diseases, enhance crops, and reshape evolution itself.",
        "field": "BIOLOGY",
        "badgeColor": "green",
        "readTime": "7 MIN READ",
        "author": "Prof. Amara Osei",
        "image": "/images/explainers/crispr-scissors.jpg",
        "content": [
            "CRISPR-Cas9 is a revolutionary gene-editing tool that allows scientists to cut, delete, and replace DNA sequences with unprecedented precision. Think of it as molecular scissors guided by a GPS.",
            "The technology was adapted from a natural defense system that bacteria use to fight viruses. When a virus attacks, bacteria capture snippets of viral DNA and store them as \"memory\" to recognize future threats.",
            "Scientists Jennifer Doudna and Emmanuelle Charpentier realized this system could be reprogrammed to target any DNA sequence, earning them the 2020 Nobel Prize in Chemistry.",
            "CRISPR has already shown promise in treating sickle cell disease, certain cancers, and inherited blindness. Clinical trials are advancing rapidly across dozens of conditions.",
            "The technology also raises profound ethical questions: should we edit human embryos? Could gene drives eliminate entire species of mosquitoes? Where do we draw the line?",
            "Next-generation tools like base editing and prime editing offer even more precise modifications, potentially correcting single-letter mutations without cutting the DNA double strand.",
        ],
        "keyInsights": [
            "CRISPR-Cas9 acts as programmable molecular scissors for DNA",
            "Adapted from bacterial immune defense systems",
            "Already treating sickle cell disease in clinical trials",
            "Raises critical ethical questions about human germline editing",
        ],
        "context": "The CRISPR system was first observed in bacteria in 1987 by Japanese researchers, but its function as an adaptive immune system wasn't understood until 2007. The pivotal 2012 paper by Doudna and Charpentier demonstrated that Cas9 could be programmed with guide RNA to cut any DNA sequence, unlocking its potential as a universal editing tool.",
        "technicalDetail": "CRISPR-Cas9 uses a guide RNA (gRNA) of ~20 nucleotides to direct the Cas9 endonuclease to a complementary DNA target adjacent to a PAM sequence (NGG for SpCas9). The enzyme creates a double-strand break, which the cell repairs via NHEJ (causing insertions/deletions) or HDR (allowing precise edits when a template is provided). Off-target effects remain a key concern, with newer high-fidelity Cas9 variants reducing unintended cuts.",
        "impact": "The first CRISPR-based therapy, Casgevy, was approved by the FDA in December 2023 for sickle cell disease. The global CRISPR therapeutics market is projected to exceed $10 billion by 2030, with applications spanning agriculture, livestock, biofuels, and disease eradication.",
    },
    {
        "id": "neural-networks",
        "title": "Neural Networks: How Machines Learn to Think",
        "subtitle": "From perceptrons to transformers — the architecture of artificial intelligence.",
        "field": "AI",
        "badgeColor": "violet",
        "readTime": "10 MIN READ",
        "author": "Dr. Kai Zhang",
        "image": "/images/explainers/neural-networks.jpg",
        "content": [
            "Artificial neural networks are computing systems inspired by the biological neural networks in animal brains. They learn by adjusting the strength of connections between artificial neurons.",
            "The simplest neural network, the perceptron, was invented in 1958. It could only solve linearly separable problems — a limitation that almost killed the field for decades.",
            "The breakthrough came with backpropagation and deep learning: stacking many layers of neurons allows networks to learn hierarchical representations of increasingly abstract features.",
            "Convolutional Neural Networks (CNNs) revolutionized computer vision by learning spatial hierarchies of features. Recurrent Neural Networks (RNNs) tackled sequences like text and speech.",
            "The transformer architecture, introduced in 2017's \"Attention Is All You Need\" paper, replaced recurrence with self-attention mechanisms, enabling massive parallelization and leading to models like GPT and BERT.",
            "Today's frontier models contain hundreds of billions of parameters and can write code, compose music, analyze medical images, and engage in complex reasoning — capabilities that seemed impossible just a decade ago.",
        ],
        "keyInsights": [
            "Neural networks learn by adjusting connection weights through backpropagation",
            "Deep learning enables hierarchical feature representation",
            "Transformers replaced recurrence with attention for massive parallelism",
            "Modern models demonstrate emergent capabilities at scale",
        ],
        "context": "The \"AI winters\" of the 1970s and 1990s nearly ended neural network research. The resurgence began in 2012 when AlexNet — a deep CNN trained on GPUs — won the ImageNet competition by a dramatic margin. This moment marked the beginning of the deep learning revolution that now powers everything from voice assistants to autonomous vehicles.",
        "technicalDetail": "A transformer processes input tokens through multi-head self-attention layers, where each token attends to every other token via scaled dot-product attention: Attention(Q,K,V) = softmax(QKᵀ/√dₖ)V. This enables O(1) sequential operations compared to O(n) for RNNs, allowing training on massive corpora. Modern LLMs use billions of parameters with techniques like rotary positional embeddings, grouped-query attention, and mixture-of-experts for efficient scaling.",
        "impact": "Large language models are reshaping every industry: automated code generation saves developers hours daily, AI copilots assist in medical diagnosis, and generative AI is projected to add $4.4 trillion annually to the global economy according to McKinsey. The race for artificial general intelligence continues to accelerate.",
    },
    {
        "id": "dark-energy",
        "title": "Dark Energy: The Force Tearing the Universe Apart",
        "subtitle": "The mysterious energy that makes up 68% of the universe and accelerates cosmic expansion.",
        "field": "EARTH & SPACE",
        "badgeColor": "orange",
        "readTime": "6 MIN READ",
        "author": "Dr. Lena Petrova",
        "image": "/images/explainers/dark-energy.jpg",
        "content": [
            "In 1998, two teams of astronomers made a shocking discovery: the universe is not just expanding — it's accelerating. Something was pushing galaxies apart faster and faster.",
            "This mysterious force was named \"dark energy.\" Despite constituting about 68% of the total energy of the universe, we know almost nothing about what it actually is.",
            "The leading hypothesis is the cosmological constant — a uniform energy density filling space homogeneously. Einstein first introduced this concept in 1917, then called it his \"biggest blunder.\"",
            "Alternative theories include quintessence (a dynamic field that varies in space and time), modifications to general relativity at cosmic scales, and effects of extra dimensions.",
            "Dark energy has profound implications for the fate of the universe. If it remains constant, the universe will expand forever, eventually reaching \"heat death.\" If it strengthens, a \"Big Rip\" could tear apart even atoms.",
            "Current experiments like the Dark Energy Survey and future missions like ESA's Euclid satellite aim to map the history of cosmic expansion with unprecedented precision.",
        ],
        "keyInsights": [
            "The universe's expansion is accelerating, driven by dark energy",
            "Dark energy constitutes ~68% of the universe's total energy",
            "The cosmological constant is the leading theoretical explanation",
            "The fate of the universe depends on dark energy's behavior over time",
        ],
        "context": "The 1998 discovery by the Supernova Cosmology Project and the High-z Supernova Search Team earned Saul Perlmutter, Brian Schmidt, and Adam Riess the 2011 Nobel Prize in Physics. They measured Type Ia supernovae — \"standard candles\" — and found that distant explosions were dimmer than expected, proving accelerated expansion.",
        "technicalDetail": "Dark energy is characterized by its equation of state parameter w = P/ρ, where P is pressure and ρ is energy density. For a cosmological constant, w = −1 exactly. Current observations constrain w to −1.03 ± 0.03, consistent with Λ but not ruling out evolving dark energy. The DESI (Dark Energy Spectroscopic Instrument) 2024 results hint at possible time variation in w, which would rule out a simple cosmological constant.",
        "impact": "Understanding dark energy could revolutionize fundamental physics. If dark energy evolves over time, it would require entirely new physics beyond general relativity. The Euclid mission (launched 2023) and the Vera Rubin Observatory will map billions of galaxies to constrain dark energy models with unprecedented precision over the next decade.",
    },
    {
        "id": "fusion-energy",
        "title": "Fusion Energy: Bottling a Star on Earth",
        "subtitle": "The quest to harness the power source of the sun for unlimited clean energy.",
        "field": "CLIMATE & ENERGY",
        "badgeColor": "gold",
        "readTime": "9 MIN READ",
        "author": "Dr. Marcus Chen",
        "image": "/images/explainers/fusion-energy.jpg",
        "content": [
            "Nuclear fusion powers every star in the universe. By fusing hydrogen atoms into helium at extreme temperatures and pressures, stars release enormous amounts of energy according to E=mc².",
            "Recreating this process on Earth requires heating hydrogen plasma to over 100 million degrees Celsius — ten times hotter than the core of the sun. No material can contain such plasma.",
            "Two main approaches exist: magnetic confinement (tokamaks like ITER) uses powerful magnetic fields to contain plasma in a donut shape, while inertial confinement (NIF) uses powerful lasers to compress fuel pellets.",
            "In December 2022, the National Ignition Facility achieved scientific breakeven for the first time — the fusion reaction produced more energy than the lasers delivered to the fuel.",
            "Private fusion companies like Commonwealth Fusion Systems, TAE Technologies, and Helion Energy are pursuing novel approaches, with some promising commercial power by the early 2030s.",
            "If achieved, fusion would provide virtually unlimited, clean energy with no greenhouse gas emissions, no long-lived radioactive waste, and fuel (deuterium) available from seawater.",
        ],
        "keyInsights": [
            "Fusion requires temperatures 10x hotter than the sun's core",
            "NIF achieved scientific breakeven in December 2022",
            "Multiple private companies target commercial fusion by the 2030s",
            "Fusion fuel (deuterium) is essentially unlimited from seawater",
        ],
        "context": "The quest for fusion energy has spanned over 70 years, beginning with the hydrogen bomb tests of the 1950s. The international ITER project in southern France, involving 35 nations, aims to demonstrate net energy gain from magnetic confinement fusion by the late 2030s, though the project has faced significant delays and cost overruns.",
        "technicalDetail": "Fusion requires overcoming the Coulomb barrier between positively charged nuclei. The D-T (deuterium-tritium) reaction has the lowest ignition temperature at ~100 million K and produces a 14.1 MeV neutron plus a 3.5 MeV alpha particle. The Lawson criterion defines the conditions for net energy gain: nτE > 10²⁰ s/m³, where n is plasma density and τE is energy confinement time. High-temperature superconducting magnets (HTS) using REBCO tape enable compact, high-field tokamaks.",
        "impact": "Commercial fusion would transform the global energy landscape. A single fusion plant could power a city with fuel from a bathtub of seawater. The fusion industry has attracted over $6 billion in private investment, with Commonwealth Fusion Systems targeting a pilot plant (SPARC) by 2025 and a commercial plant (ARC) by the early 2030s.",
    },
    {
        "id": "blockchain-consensus",
        "title": "Blockchain Consensus: Trust Without Authority",
        "subtitle": "How distributed networks agree on truth without a central authority.",
        "field": "COMPUTER SCIENCE",
        "badgeColor": "red",
        "readTime": "7 MIN READ",
        "author": "Prof. Sara Nakamoto",
        "image": "/images/explainers/blockchain-consensus.jpg",
        "content": [
            "The fundamental challenge of distributed systems is the Byzantine Generals Problem: how can multiple parties agree on a course of action when some may be unreliable or malicious?",
            "Blockchain solves this through consensus mechanisms — protocols that allow a network of computers to agree on the state of a shared ledger without trusting any single participant.",
            "Proof of Work (PoW), used by Bitcoin, requires miners to solve computationally expensive puzzles. The first to solve gets to add the next block. This is secure but energy-intensive.",
            "Proof of Stake (PoS), adopted by Ethereum in 2022, selects validators based on their staked cryptocurrency. It's ~99.95% more energy-efficient than PoW while maintaining security.",
            "Novel consensus mechanisms continue to emerge: Proof of History (Solana), Directed Acyclic Graphs (IOTA), and Byzantine Fault Tolerant protocols (Cosmos) each offer different tradeoffs.",
            "Beyond cryptocurrency, consensus mechanisms enable decentralized identity, supply chain tracking, voting systems, and any application requiring trustless coordination between strangers.",
        ],
        "keyInsights": [
            "Consensus mechanisms solve the Byzantine Generals Problem",
            "Proof of Stake is ~99.95% more energy-efficient than Proof of Work",
            "Multiple novel mechanisms offer different performance tradeoffs",
            "Applications extend far beyond cryptocurrency",
        ],
        "context": "The Byzantine Generals Problem was formally described by Lamport, Shostak, and Pease in 1982. Satoshi Nakamoto's 2008 Bitcoin whitepaper provided the first practical solution using Proof of Work, enabling a trustless peer-to-peer electronic cash system that has since grown into a trillion-dollar asset class.",
        "technicalDetail": "PoW security relies on the assumption that no single entity controls >50% of network hash rate. The difficulty adjusts every 2,016 blocks (~2 weeks) to maintain ~10-minute block times. PoS replaces computational work with economic stake: validators are selected pseudo-randomly weighted by their stake, and slashing conditions penalize dishonest behavior. Ethereum's Casper FFG achieves finality through a two-round voting process with a 2/3 supermajority requirement.",
        "impact": "Decentralized consensus is enabling new forms of governance, finance (DeFi), and digital ownership (NFTs). Enterprise blockchain adoption is growing in supply chain management, cross-border payments, and digital identity. The total value locked in DeFi protocols has exceeded $100 billion, demonstrating real-world demand for trustless financial infrastructure.",
    },
    {
        "id": "epigenetics",
        "title": "Epigenetics: The Code Above the Code",
        "subtitle": "How your environment rewrites gene expression without changing DNA itself.",
        "field": "BIOLOGY",
        "badgeColor": "green",
        "readTime": "8 MIN READ",
        "author": "Dr. Hana Kimura",
        "image": "/images/explainers/epigenetics.jpg",
        "content": [
            "Your DNA sequence is fixed at birth, but how your genes are read is surprisingly flexible. Epigenetics studies chemical modifications that turn genes on and off without altering the underlying code.",
            "DNA methylation — attaching small methyl groups to DNA — is one of the most studied mechanisms. Heavy methylation silences a gene, while demethylation can reactivate it.",
            "Histone modifications wrap and unwrap DNA around protein spools, controlling which stretches of the genome are accessible to the cell's reading machinery.",
            "Diet, stress, toxins, and even social interactions can trigger epigenetic changes. Some of these changes are heritable, passed from parent to offspring across generations.",
            "Epigenetic therapies are emerging in cancer treatment — drugs that reverse abnormal methylation patterns can reactivate tumor-suppressor genes that cancer cells had silenced.",
            "The field challenges the old nature-vs-nurture debate: your lived experience literally shapes which parts of your genetic blueprint are active.",
        ],
        "keyInsights": [
            "Epigenetic marks control gene expression without changing DNA sequence",
            "Environmental factors like diet and stress can alter your epigenome",
            "Some epigenetic changes pass across generations",
            "Epigenetic drugs are showing promise in cancer therapy",
        ],
        "context": "Conrad Waddington coined the term \"epigenetics\" in 1942, but the molecular mechanisms weren't understood until the 1990s. The Dutch Hunger Winter study provided compelling evidence: children born during the 1944-45 famine showed epigenetic changes affecting metabolism that persisted for generations, demonstrating that environmental trauma could be biologically inherited.",
        "technicalDetail": "DNA methylation occurs primarily at CpG dinucleotides, catalyzed by DNA methyltransferases (DNMTs). Methylation of promoter CpG islands typically silences transcription by recruiting methyl-CpG binding proteins that compact chromatin. Histone modifications include acetylation (H3K27ac — activating), methylation (H3K4me3 — activating; H3K27me3 — repressive), and phosphorylation. The \"histone code\" of combinatorial modifications creates a regulatory layer readable by chromatin remodeling complexes.",
        "impact": "Five epigenetic drugs are currently FDA-approved for cancer treatment, including azacitidine and decitabine for myelodysplastic syndromes. The epigenetic diagnostics market is growing rapidly, with liquid biopsy tests using methylation patterns to detect cancers early. Understanding transgenerational epigenetic inheritance could reshape public health approaches to poverty, nutrition, and environmental exposure.",
    },
    {
        "id": "quantum-entanglement",
        "title": "Quantum Entanglement: Spooky Action at a Distance",
        "subtitle": "Two particles linked across the universe — measuring one instantly affects the other.",
        "field": "QUANTUM PHYSICS",
        "badgeColor": "cyan",
        "readTime": "9 MIN READ",
        "author": "Dr. Ravi Sharma",
        "image": "/images/explainers/quantum-entanglement.jpg",
        "content": [
            "When two particles become entangled, measuring one instantly determines the state of the other — no matter how far apart they are. Einstein famously called this \"spooky action at a distance.\"",
            "Entanglement is created when particles interact in specific ways — for example, splitting a photon into two via a nonlinear crystal produces a pair with correlated polarizations.",
            "Bell's theorem (1964) and subsequent experiments proved that entanglement is real and not explained by hidden local variables. Nature is genuinely nonlocal at the quantum level.",
            "Quantum teleportation uses entanglement to transfer quantum states between distant particles. It doesn't move matter or information faster than light, but it enables fundamentally secure communication.",
            "Quantum key distribution (QKD) harnesses entanglement for cryptography: any eavesdropping attempt disturbs the entangled state, alerting both parties immediately.",
            "Researchers have demonstrated entanglement over 1,200 km using the Micius satellite, paving the way for a future quantum internet.",
        ],
        "keyInsights": [
            "Entangled particles share correlated states regardless of distance",
            "Bell experiments ruled out classical explanations for entanglement",
            "Quantum teleportation transfers states, not matter or energy",
            "Satellite-based entanglement spans over 1,200 km",
        ],
        "context": "Einstein, Podolsky, and Rosen published their famous EPR paradox in 1935, arguing that quantum mechanics must be incomplete because entanglement seemed to violate locality. John Bell's 1964 inequality provided a testable prediction, and Alain Aspect's 1982 experiments confirmed quantum nonlocality. The 2022 Nobel Prize in Physics was awarded to Aspect, Clauser, and Zeilinger for their pioneering work on entanglement.",
        "technicalDetail": "An entangled Bell state |Φ⁺⟩ = (|00⟩ + |11⟩)/√2 has the property that measuring one qubit in any basis instantly determines the outcome of measuring the partner in the same basis, with correlations exceeding the Bell inequality bound of 2 (quantum limit: 2√2 ≈ 2.83). Entanglement swapping enables teleportation: Alice and Bob share entangled pairs, Alice performs a Bell measurement on her particle and the teleported state, then sends 2 classical bits to Bob, who applies a correction to recover the state.",
        "impact": "China's quantum satellite network (Micius) has demonstrated intercontinental QKD, and several countries are building quantum communication infrastructure. The quantum internet could enable unhackable communications, distributed quantum computing, and synchronized atomic clocks. Private companies like Toshiba, ID Quantique, and PsiQuantum are commercializing quantum networking technologies.",
    },
    {
        "id": "neuroplasticity",
        "title": "Neuroplasticity: The Brain That Rewires Itself",
        "subtitle": "How your brain physically changes structure in response to learning and experience.",
        "field": "BIOLOGY",
        "badgeColor": "green",
        "readTime": "7 MIN READ",
        "author": "Dr. Sofia Andersson",
        "image": "/images/explainers/neuroplasticity.jpg",
        "content": [
            "For most of the 20th century, scientists believed the adult brain was fixed. We now know it continually rewires itself — forming new connections, pruning unused ones, and even generating new neurons.",
            "Every time you learn a skill, synapses strengthen through a process called long-term potentiation (LTP). Repeated practice physically thickens the neural pathways involved.",
            "London taxi drivers famously have enlarged hippocampi — the brain region responsible for spatial memory — compared to bus drivers who follow fixed routes.",
            "After a stroke, undamaged brain regions can take over functions from damaged areas through intensive rehabilitation, demonstrating remarkable structural flexibility.",
            "Negative plasticity exists too: chronic stress shrinks the prefrontal cortex (decision-making) while enlarging the amygdala (fear response), explaining anxiety disorders.",
            "Mindfulness meditation has been shown to increase cortical thickness in attention-related areas after just eight weeks of practice.",
        ],
        "keyInsights": [
            "The adult brain continuously forms and prunes neural connections",
            "Repeated practice physically strengthens synaptic pathways",
            "Brain regions can compensate for damage through reorganization",
            "Both positive and negative experiences reshape brain structure",
        ],
        "context": "The dogma of the \"fixed adult brain\" dominated neuroscience for over a century. Santiago Ramón y Cajal, the father of modern neuroscience, stated in 1928 that nerve paths were \"fixed, ended, immutable.\" The paradigm shift began with Michael Merzenich's work in the 1980s showing that cortical maps reorganize after sensory changes, and was cemented by the discovery of adult neurogenesis in the hippocampus in the 1990s.",
        "technicalDetail": "Long-term potentiation (LTP) involves NMDA receptor activation, calcium influx, and subsequent AMPA receptor insertion at the postsynaptic membrane, strengthening synaptic transmission. Structural plasticity includes dendritic spine growth (within hours of learning), axonal sprouting, and synaptogenesis. Adult neurogenesis occurs primarily in the subgranular zone of the hippocampal dentate gyrus and the subventricular zone, producing ~700 new neurons daily in the human hippocampus.",
        "impact": "Neuroplasticity research has transformed rehabilitation medicine. Constraint-induced movement therapy (CIMT) forces use of stroke-affected limbs, driving cortical reorganization. Brain-computer interfaces (BCIs) leverage plasticity to help paralyzed patients control devices with thought. The mindfulness and cognitive training industries have grown into multi-billion dollar markets, though the transfer of training benefits to general cognition remains debated.",
    },
]


# ── Field metadata ────────────────────────────────────────────────────────────

FIELDS_SEED = [
    "PHYSICS", "CHEMISTRY", "BIOLOGY", "MEDICINE", "EARTH & SPACE",
    "COMPUTER SCIENCE", "AI", "ROBOTICS", "ENGINEERING",
    "MATHEMATICS & DATA", "CLIMATE & ENERGY",
]

FIELD_ICONS_SEED = {
    "PHYSICS":           "⚛️",
    "CHEMISTRY":         "🧪",
    "BIOLOGY":           "🧬",
    "MEDICINE":          "💊",
    "EARTH & SPACE":     "🌍",
    "COMPUTER SCIENCE":  "💻",
    "AI":                "🤖",
    "ROBOTICS":          "🦾",
    "ENGINEERING":       "⚙️",
    "MATHEMATICS & DATA":"📐",
    "CLIMATE & ENERGY":  "🌱",
}

FIELD_COLORS_SEED = {
    "PHYSICS":           "cyan",
    "CHEMISTRY":         "orange",
    "BIOLOGY":           "green",
    "MEDICINE":          "red",
    "EARTH & SPACE":     "violet",
    "COMPUTER SCIENCE":  "cyan",
    "AI":                "violet",
    "ROBOTICS":          "orange",
    "ENGINEERING":       "gold",
    "MATHEMATICS & DATA":"cyan",
    "CLIMATE & ENERGY":  "green",
}

# Maps each research field to its hero/banner image served via StaticFiles.
FIELD_IMAGES_SEED = {
    "PHYSICS":           "/images/research/physics.jpg",
    "CHEMISTRY":         "/images/research/chemistry.jpg",
    "BIOLOGY":           "/images/research/biology.jpg",
    "MEDICINE":          "/images/research/medicine.jpg",
    "EARTH & SPACE":     "/images/research/earth-space.jpg",
    "COMPUTER SCIENCE":  "/images/research/computer-science.jpg",
    "AI":                "/images/research/ai.jpg",
    "ROBOTICS":          "/images/research/robotics.jpg",
    "ENGINEERING":       "/images/research/engineering.jpg",
    "MATHEMATICS & DATA":"/images/research/mathematics.jpg",
    "CLIMATE & ENERGY":  "/images/research/climate-energy.jpg",
}


# ── Research articles ─────────────────────────────────────────────────────────

RESEARCH_ARTICLES_SEED = [
    {
        "id": "a1",
        "title": "Topological Qubits Achieve 99.9% Fidelity",
        "abstract": "Microsoft Research demonstrates record-breaking qubit stability using Majorana fermions in topological superconductors, opening a new chapter for fault-tolerant quantum computing.",
        "field": "PHYSICS",
        "author": "Dr. Sarah Chen",
        "date": "2025-03-15",
        "readTime": "12 min",
        "image": "/images/research/physics.jpg",
        "content": [
            "Topological quantum computing has long been considered the holy grail of quantum information science. Unlike conventional qubits that are fragile and error-prone, topological qubits encode information in the global properties of a quantum system, making them inherently resistant to local noise and perturbation.",
            "The key innovation lies in using Majorana fermions — exotic particles that are their own antiparticles. When these particles are braided around each other, they perform quantum computations that are inherently protected from local noise. This braiding operation is topologically protected, meaning small perturbations cannot corrupt the computation.",
            "Microsoft's latest breakthrough achieved 99.9% gate fidelity, surpassing the threshold needed for practical quantum error correction. This means that topological quantum computers could require orders of magnitude fewer physical qubits than surface-code approaches.",
            "The implications are staggering: drug discovery simulations that would take classical computers millions of years could be completed in hours. Materials science, cryptography, and optimization problems all stand to benefit from this revolutionary advance in quantum hardware.",
        ],
        "quotes": ['"This is the moment topological quantum computing goes from theory to engineering." — Dr. Chetan Nayak, Microsoft'],
        "keyFindings": [
            "99.9% gate fidelity achieved with topological qubits",
            "Majorana fermion braiding demonstrated at scale",
            "1000x fewer physical qubits needed vs. conventional approaches",
            "Path to fault-tolerant quantum computing now clear",
        ],
        "relatedTopics": ["Quantum Error Correction", "Majorana Fermions", "Topological Insulators"],
    },
    {
        "id": "a6",
        "title": "Room-Temperature Superconductor Confirmed by Three Independent Labs",
        "abstract": "LK-99 successor material shows zero resistance at 15°C and ambient pressure, verified across MIT, Max Planck, and RIKEN.",
        "field": "PHYSICS",
        "author": "Dr. Elena Volkov",
        "date": "2025-02-28",
        "readTime": "11 min",
        "image": "/images/research/physics.jpg",
        "content": [
            "Three independent laboratories — MIT, Max Planck Institute, and RIKEN — have confirmed that a new copper-doped lead apatite derivative exhibits true superconductivity at room temperature and ambient pressure.",
            "The material, developed by a team at Seoul National University, builds on the controversial LK-99 announcement of 2023. Years of refinement to the synthesis process eliminated the impurities that caused earlier samples to fail.",
            "Room-temperature superconductivity represents one of the most sought-after materials science breakthroughs of the century. Current superconductors require cooling to near absolute zero, limiting practical applications to specialized industrial uses.",
            "Commercial implications are transformative: lossless power transmission grids, ultra-high-speed maglev trains, compact MRI machines, and quantum computers operating at room temperature all become feasible.",
        ],
        "quotes": ['"After 30 years, I can finally say it without hesitation: room-temperature superconductivity is real." — Prof. Jun Nagamatsu, Aoyama Gakuin University'],
        "keyFindings": [
            "Zero resistance confirmed at 15°C and 1 atm pressure",
            "Verified by three independent international laboratories",
            "Based on copper-doped lead apatite synthesis",
            "Enables lossless power transmission and compact MRI",
        ],
        "relatedTopics": ["Superconductivity", "BCS Theory", "Cooper Pairs", "Meissner Effect"],
    },
    {
        "id": "a2",
        "title": "AI Discovers New Antibiotic Class After 60-Year Gap",
        "abstract": "Deep learning model screens 1.2 billion molecular candidates to identify halicin derivatives effective against drug-resistant bacteria.",
        "field": "MEDICINE",
        "author": "Dr. James Liu",
        "date": "2025-03-10",
        "readTime": "9 min",
        "image": "/images/research/medicine.jpg",
        "content": [
            "For the first time in over 60 years, scientists have discovered an entirely new class of antibiotics — and artificial intelligence made it possible.",
            "A deep learning model trained on molecular structures and antibiotic activity screened over 1.2 billion candidate molecules in three days — a task that would have taken centuries using traditional methods.",
            "The model identified halicin derivatives that kill bacteria through a novel mechanism: disrupting the electrochemical gradient across bacterial membranes. This mechanism is fundamentally different from all existing antibiotics, meaning resistance is far harder to develop.",
            "The discovery is urgent: antimicrobial resistance kills 1.3 million people annually and is projected to become the leading cause of death globally by 2050 without new antibiotics.",
        ],
        "quotes": ['"AI didn\'t just accelerate drug discovery — it found something we never would have found with traditional methods." — Prof. Regina Barzilay, MIT'],
        "keyFindings": [
            "New antibiotic class discovered for first time since 1987",
            "Novel membrane-disruption mechanism resists resistance development",
            "1.2 billion molecules screened in 72 hours",
            "Effective against MRSA, C. diff, and pan-resistant Acinetobacter",
        ],
        "relatedTopics": ["Antimicrobial Resistance", "Drug Discovery", "Deep Learning in Medicine"],
    },
    {
        "id": "a3",
        "title": "Humanoid Robots Begin Autonomous Construction Work",
        "abstract": "Boston Dynamics' Atlas units complete 4-hour unsupervised structural tasks at real construction sites.",
        "field": "ROBOTICS",
        "author": "Dr. Priya Nair",
        "date": "2025-03-05",
        "readTime": "8 min",
        "image": "/images/research/robotics.jpg",
        "content": [
            "Boston Dynamics' Atlas robots have achieved a landmark milestone: completing four-hour autonomous construction tasks at real job sites with no human supervision.",
            "The robots performed structural framing, drywall installation, and material transport — tasks requiring dynamic balance, tool manipulation, and adaptive decision-making in unpredictable environments.",
            "The breakthrough combines reinforcement learning for physical control with large language model planning for task decomposition. The LLM interprets high-level instructions and breaks them into physical actions the robot can execute.",
            "Construction sites present extreme challenges: irregular surfaces, variable lighting, unexpected obstacles, and the need to handle materials of different weights and textures.",
        ],
        "quotes": ['"We\'re not replacing construction workers — we\'re augmenting them for dangerous and repetitive tasks." — Robert Playter, CEO, Boston Dynamics'],
        "keyFindings": [
            "4-hour autonomous wall framing completed",
            "Adaptive manipulation with irregular materials",
            "Integration of LLM planning with physical control",
            "Addresses 500,000 unfilled construction jobs in US",
        ],
        "relatedTopics": ["Humanoid Robots", "Construction Technology", "Reinforcement Learning"],
    },
    {
        "id": "a7",
        "title": "DeepMind Solves Protein-Protein Interaction Prediction",
        "abstract": "AlphaFold 4 predicts multi-protein complex formations with 95% accuracy, unlocking drug target discovery.",
        "field": "BIOLOGY",
        "author": "Dr. Ana Torres",
        "date": "2025-03-01",
        "readTime": "10 min",
        "image": "/images/research/biology.jpg",
        "content": [
            "DeepMind's AlphaFold 4 has solved one of biology's grand challenges: predicting how multiple proteins interact and assemble into functional complexes with near-experimental accuracy.",
            "While AlphaFold 2 revolutionized single-protein structure prediction, most biological functions depend on complex interactions between multiple proteins. AlphaFold 4 predicts these assemblies with 95% accuracy.",
            "The model was trained on cryo-electron microscopy data of over 100,000 protein complexes, learning the subtle thermodynamic and geometric rules that govern protein-protein recognition and binding.",
            "This capability is transformative for drug discovery. Understanding how disease-related proteins interact allows researchers to design drugs that precisely disrupt pathological interactions while leaving healthy ones intact.",
        ],
        "quotes": ['"This is the missing piece that turns structural biology into a truly predictive science." — Demis Hassabis, CEO, DeepMind'],
        "keyFindings": [
            "95% accuracy for multi-protein complex prediction",
            "Trained on 100,000+ cryo-EM structures",
            "Already identified 12 novel drug targets",
            "Revolutionizes structure-based drug design",
        ],
        "relatedTopics": ["Protein Folding", "Drug Discovery", "Structural Biology"],
    },
    {
        "id": "a16",
        "title": "Synthetic Biology Creates First Self-Replicating Artificial Cell",
        "abstract": "Craig Venter Institute achieves minimal artificial cell that grows, divides, and evolves with only 473 genes.",
        "field": "BIOLOGY",
        "author": "Dr. Kim Novak",
        "date": "2025-02-05",
        "readTime": "12 min",
        "image": "/images/research/biology.jpg",
        "content": [
            "Scientists at the Craig Venter Institute have created the first truly self-replicating artificial cell — a synthetic organism built from scratch that can grow, divide, and even evolve over multiple generations.",
            "The organism, JCVI-syn3.1, contains only 473 genes — the minimal set needed for independent life. Every gene was chemically synthesized and assembled into a complete genome that was then booted inside an empty cell membrane.",
            "Unlike previous synthetic biology achievements that modified existing organisms, this cell was designed from a blank slate, giving researchers complete control over every aspect of its biology and behavior.",
            "The breakthrough has profound implications for biotechnology: synthetic cells could be programmed to produce medicines, biofuels, or materials on demand, serving as living factories with capabilities designed entirely by humans.",
        ],
        "quotes": ['"We have crossed the threshold from reading the genetic code to writing it from scratch." — Dr. Craig Venter'],
        "keyFindings": [
            "Self-replicating artificial cell created with 473 genes",
            "Complete genome chemically synthesized",
            "Cell grows, divides, and evolves independently",
            "Foundation for programmable living factories",
        ],
        "relatedTopics": ["Synthetic Biology", "Minimal Genome", "Bioengineering", "Origin of Life"],
    },
    {
        "id": "a8",
        "title": "Solid-State Batteries Enter Mass Production",
        "abstract": "Toyota begins commercial production of solid-state batteries with 1,200km range and 10-minute charging.",
        "field": "ENGINEERING",
        "author": "Dr. Yuki Tanaka",
        "date": "2025-03-18",
        "readTime": "7 min",
        "image": "/images/research/engineering.jpg",
        "content": [
            "Toyota has begun mass production of solid-state batteries at its Himeji facility, marking the beginning of a new era for electric vehicles and energy storage technology worldwide.",
            "The batteries use a sulfide-based solid electrolyte instead of liquid, enabling higher energy density (500 Wh/kg vs 250 Wh/kg for current lithium-ion), faster charging, and dramatically improved safety with no flammable liquids.",
            "Initial production will supply Toyota's new flagship EV, offering 1,200km range and 10% to 80% charging in just 10 minutes — eliminating the two biggest barriers to widespread EV adoption.",
            "The technology also enables new form factors: batteries can be made thinner, lighter, and in arbitrary shapes, opening possibilities for wearable electronics, aerospace applications, and grid-scale storage.",
        ],
        "quotes": ['"Solid-state batteries will do for EVs what lithium-ion did for smartphones." — Akio Toyoda, Toyota Chairman'],
        "keyFindings": [
            "500 Wh/kg energy density (2x current Li-ion)",
            "10-minute fast charging to 80% capacity",
            "Mass production at commercial scale achieved",
            "Eliminates flammability risks of liquid electrolytes",
        ],
        "relatedTopics": ["Battery Technology", "Electric Vehicles", "Energy Storage"],
    },
    {
        "id": "a9",
        "title": "Riemann Hypothesis Proof Verified by Mathematical Community",
        "abstract": "After 3 years of scrutiny, the proof by Dr. Yitang Zhang is accepted, solving the 165-year-old problem.",
        "field": "MATHEMATICS & DATA",
        "author": "Dr. Michael Torres",
        "date": "2025-02-20",
        "readTime": "13 min",
        "image": "/images/research/mathematics.jpg",
        "content": [
            "The mathematical community has formally accepted a proof of the Riemann Hypothesis, one of the seven Millennium Prize Problems and arguably the most important unsolved problem in mathematics for over a century.",
            "Dr. Yitang Zhang, known for his breakthrough on bounded gaps between primes, submitted the proof in 2022. After three years of intense verification by dozens of leading mathematicians worldwide, no errors have been found.",
            "The Riemann Hypothesis concerns the distribution of prime numbers and the zeros of the Riemann zeta function. Its proof has immediate implications across mathematics, theoretical physics, and modern cryptography.",
            "RSA cryptography, which secures most internet communications, relies on the difficulty of factoring large numbers — intimately connected to prime distribution. The proof's implications for cybersecurity are still being assessed by intelligence agencies.",
        ],
        "quotes": ['"This is the Mount Everest of mathematics. Zhang has reached the summit." — Prof. Terence Tao, UCLA'],
        "keyFindings": [
            "Proof verified by 40+ independent mathematicians",
            "Implications for prime number distribution fully characterized",
            "Potential impact on RSA cryptography security assessment",
            "$1 million Millennium Prize awarded",
        ],
        "relatedTopics": ["Number Theory", "Zeta Function", "Prime Distribution"],
    },
    {
        "id": "a10",
        "title": "Mars Sample Return: First Martian Soil Arrives on Earth",
        "abstract": "ESA-NASA joint mission successfully delivers 350g of Perseverance-collected Mars samples to Utah facility.",
        "field": "EARTH & SPACE",
        "author": "Dr. Clara Novak",
        "date": "2025-03-20",
        "readTime": "8 min",
        "image": "/images/research/earth-space.jpg",
        "content": [
            "In a historic achievement for space exploration, the first samples of Martian soil have safely landed on Earth, completing a mission that took over a decade of planning and flawless execution.",
            "The Mars Sample Return mission, a joint effort between ESA and NASA, retrieved 30 sealed tubes cached by the Perseverance rover across Jezero Crater — an ancient lake bed believed to have once harbored microbial life.",
            "The 350 grams of material include sedimentary rocks, igneous samples, and atmospheric gases. Preliminary analysis suggests the presence of complex organic molecules, though biological origin has not yet been confirmed.",
            "Samples are being distributed to 200 laboratories in 30 countries for analysis, using instruments far more sensitive than any rover could carry. Results are expected to transform our understanding of Mars's past habitability.",
        ],
        "quotes": ['"Holding a piece of Mars in your hands — it changes your perspective on what\'s possible." — Dr. Laurie Leshin, JPL Director'],
        "keyFindings": [
            "350g of Martian material safely returned to Earth",
            "Complex organic molecules detected in preliminary analysis",
            "200 labs in 30 countries conducting detailed analysis",
            "Samples from ancient lake bed with habitability potential",
        ],
        "relatedTopics": ["Mars Exploration", "Astrobiology", "Sample Return Missions"],
    },
    {
        "id": "a11",
        "title": "Catalytic CO2 Conversion Achieves Industrial Scale",
        "abstract": "Carbon Engineering's new catalyst converts atmospheric CO2 to jet fuel at $80/barrel, competitive with fossil fuels.",
        "field": "CHEMISTRY",
        "author": "Dr. Amara Osei",
        "date": "2025-03-14",
        "readTime": "9 min",
        "image": "/images/research/chemistry.jpg",
        "content": [
            "Carbon Engineering has achieved a breakthrough that could transform the fight against climate change: converting atmospheric CO2 into synthetic jet fuel at costs competitive with fossil fuel extraction.",
            "The new iron-cobalt catalyst operates at lower temperatures and pressures than previous approaches, dramatically reducing energy requirements. The process captures CO2 directly from ambient air and combines it with green hydrogen.",
            "At $80 per barrel equivalent, synthetic aviation fuel is now within the price range of conventional jet fuel, removing the economic barrier to decarbonizing the aviation industry — one of the hardest sectors to electrify.",
            "The company has broken ground on a facility in Texas that will produce 100 million liters of synthetic fuel annually, enough to power 10,000 transatlantic flights per year with net-zero carbon emissions.",
        ],
        "quotes": ['"We\'re turning air pollution into aviation fuel. The circular carbon economy is here." — Steve Oldham, CEO, Carbon Engineering'],
        "keyFindings": [
            "$80/barrel synthetic fuel from atmospheric CO2",
            "New catalyst reduces energy requirements by 60%",
            "100 million liter/year facility under construction",
            "Net-zero aviation fuel at fossil-fuel-competitive prices",
        ],
        "relatedTopics": ["Carbon Capture", "Catalysis", "Sustainable Aviation"],
    },
    {
        "id": "a17",
        "title": "Post-Quantum Cryptography Standard Deployed Across Major Browsers",
        "abstract": "NIST's ML-KEM algorithm now protects 80% of web traffic against future quantum computer attacks.",
        "field": "COMPUTER SCIENCE",
        "author": "Dr. Anil Gupta",
        "date": "2025-03-28",
        "readTime": "8 min",
        "image": "/images/research/computer-science.jpg",
        "content": [
            "All major web browsers have completed the rollout of post-quantum cryptographic algorithms, protecting an estimated 80% of global internet traffic against attacks from future quantum computers.",
            "The transition centers on ML-KEM (Module-Lattice Key Encapsulation Mechanism), selected by NIST after an 8-year evaluation process. The algorithm's security is based on the hardness of lattice problems, which remain intractable even for quantum computers.",
            "The deployment uses a hybrid approach, combining traditional elliptic curve cryptography with ML-KEM to ensure security against both classical and quantum attacks during the transition period.",
            "The urgency of the transition is driven by \"harvest now, decrypt later\" attacks, where adversaries capture encrypted traffic today intending to decrypt it once quantum computers become powerful enough.",
        ],
        "quotes": ['"We\'re not just protecting today\'s data — we\'re protecting today\'s secrets from tomorrow\'s computers." — Dr. Dustin Moody, NIST'],
        "keyFindings": [
            "80% of web traffic now quantum-resistant",
            "ML-KEM deployed in hybrid mode across all major browsers",
            "Protects against \"harvest now, decrypt later\" attacks",
            "Lattice-based security intractable for quantum computers",
        ],
        "relatedTopics": ["Post-Quantum Cryptography", "Lattice Problems", "TLS Protocol", "NIST Standards"],
    },
]


# ── Blog posts ────────────────────────────────────────────────────────────────
# Matches the BlogPost interface in blog.ts.
# type: "explainer" | "article" | "simulation"

BLOG_POSTS_SEED = [
    {
        "id": "the-future-of-quantum-computing",
        "title": "The Future of Quantum Computing",
        "subtitle": "How qubits are reshaping the landscape of computation and cryptography.",
        "description": "An in-depth look at quantum advantage, error correction, and the path to scalable quantum machines.",
        "author": {
            "name": "Dr. Aris Vance",
            "role": "Quantum Physicist",
            "avatar": "https://i.pravatar.cc/150?u=aris",
            "bio": "Dr. Vance is a leading researcher in quantum error correction and topology.",
        },
        "publishDate": "Oct 12, 2026",
        "readingTime": "8 MIN READ",
        "coverImage": "https://images.unsplash.com/photo-1635070041078-e363dbe005cb?q=80&w=2000&auto=format&fit=crop",
        "field": "COMPUTING",
        "badgeColor": "cyan",
        "tags": ["Quantum", "Hardware", "Cryptography"],
        "keyInsights": [
            "Quantum advantage has been demonstrated in specific simulation tasks.",
            "Logical qubits require thousands of physical qubits for error correction.",
            "Post-quantum cryptography is becoming a priority for global security.",
        ],
        "type": "article",
        "content": """## Introduction

The promise of quantum computing has moved from theoretical physics to engineering reality. While classical computers rely on bits (0s and 1s), quantum computers use qubits, which can exist in multiple states simultaneously thanks to superposition.

### The Power of Superposition

Superposition allows quantum algorithms to evaluate many possibilities at once. However, harnessing this power requires extreme isolation. Even a stray photon can cause "decoherence," destroying the quantum state.

## Challenges in Scaling

The biggest hurdle today is **quantum error correction**. Physical qubits are noisy and prone to errors.

1. **Decoherence**: Environmental noise disrupts qubit states.
2. **Gate Fidelity**: Operations on qubits must be extremely precise.
3. **Cooling**: Superconducting qubits require temperatures near absolute zero.

### Error Correction

To create one reliable "logical" qubit, we might need up to 1,000 physical qubits. This overhead is massive, but recent breakthroughs in topological error correction offer a path forward.

## Looking Ahead

Over the next decade, we expect to see hybrid quantum-classical algorithms solving specific problems in materials science, drug discovery, and optimization, long before fault-tolerant quantum computers are fully realized.""",
    },
    {
        "id": "crispr-and-the-gene-editing-revolution",
        "title": "CRISPR and the Gene Editing Revolution",
        "subtitle": "Precision medicine is rewriting the code of life.",
        "description": "Exploring the mechanisms, ethical implications, and future applications of CRISPR-Cas9 technology.",
        "author": {
            "name": "Elena Rostova",
            "role": "Bioinformatician",
            "avatar": "https://i.pravatar.cc/150?u=elena",
            "bio": "Elena focuses on computational models for targeted gene delivery systems.",
        },
        "publishDate": "Nov 04, 2026",
        "readingTime": "6 MIN READ",
        "coverImage": "https://images.unsplash.com/photo-1530026405186-ed1f139313f8?q=80&w=2000&auto=format&fit=crop",
        "field": "BIOLOGY",
        "badgeColor": "green",
        "tags": ["Genomics", "Medicine", "CRISPR"],
        "keyInsights": [
            "CRISPR allows for highly precise, cost-effective DNA editing.",
            "Off-target effects remain a challenge for clinical applications.",
            "Ethical frameworks are struggling to keep pace with the technology.",
        ],
        "type": "article",
        "content": """## Introduction

CRISPR-Cas9 has transformed from a laboratory curiosity into one of the most powerful tools in modern medicine. By allowing scientists to edit DNA with unprecedented precision, it has opened doors that were previously considered permanently shut.

## How It Works

The system uses a guide RNA to direct the Cas9 protein to a specific DNA sequence, where it makes a precise cut. The cell's own repair machinery then either disables the gene (via NHEJ) or corrects it using a provided template (via HDR).

## Clinical Progress

The first CRISPR-based therapy, Casgevy, received FDA approval in December 2023 for sickle cell disease — a landmark moment for the field. Dozens of other trials are underway targeting cancers, inherited blindness, and cardiovascular disease.

## Ethical Frontiers

The technology's power raises profound questions: germline editing, ecological gene drives, and enhancement applications all demand robust international governance frameworks that are still taking shape.""",
    },
]