# FieldKit Framework Philosophy

## Vision

FieldKit exists to democratize sophisticated business automation for small service businesses. Rather than offering a rigid SaaS product, we provide a framework that enables rapid development of tailored solutions — each client gets a custom system that fits their exact needs, built on proven patterns.

## Core Principles

### 1. Spec-First Development

**Requirements before implementation.** Technology-agnostic specifications define WHAT we're building and WHY, separating intent from implementation details.

- Constitution establishes governing principles
- Specifications capture user needs without technical bias
- Technical plans come after requirements are validated
- Implementation follows validated plans

### 2. Client Ownership & Independence

**Each client owns their solution.** We build independent codebases, not multi-tenant SaaS.

- Clients get full source code
- No vendor lock-in
- Freedom to evolve independently
- Complete data ownership
- Can hire others to maintain or extend

### 3. Proven Patterns Over Greenfield

**Learn from real implementations.** Reference implementations are extracted from working client solutions, not theoretical designs.

- Build real client solution first
- Extract patterns that actually worked
- Reference implementations are battle-tested
- Continuous improvement from client feedback

### 4. Industry-Specific, Not Generic

**One size fits nobody.** Small businesses have wildly different needs based on their industry.

- Construction contractors need project showcases
- Restaurants need daily special promotion
- Salons need style transformation content
- Reference implementations per industry vertical
- Deep industry understanding required

### 5. Privacy-First Design

**Customer privacy is non-negotiable.** Business clients trust us with their customer data.

- Privacy protection baked into every feature
- Metadata stripping, PII detection
- Human verification before publication
- Clear consent requirements
- Industry-specific privacy considerations

### 6. Cost-Conscious Architecture

**Small businesses can't afford unlimited AI spend.** Budget constraints drive better design.

- Hard daily/monthly cost limits
- Model selection based on task complexity
- Efficient prompt engineering
- Batch processing where possible
- Real-time cost tracking and alerts

### 7. Human-in-the-Loop

**AI assists, humans decide.** Critical business communications require human judgment.

- Approval workflows for customer-facing content
- AI drafts, human refines
- Learn from human edits
- Respect domain expertise
- Augment, don't replace

### 8. Self-Hosted by Default

**The client's data lives on the client's hardware.** FieldKit is designed to run on a dedicated Mac Mini at the client's location, running the Hermes Agent runtime.

- AI inference now routes to a cloud model provider (Anthropic or OpenAI) via Hermes — no longer local-only; see `platform/.specify/003-hermes-runtime/spec.md` for the Mac Mini → Cloud pivot
- Budget-governed cost structure (Gate 3) — now includes per-token API costs to the chosen model provider, not just hardware
- Data never leaves the client's premises except for model calls to the chosen provider
- Hardware transfers to client on project completion
- Full independence from framework after handoff

### 9. Test-Driven Development

**Every feature is proven by automated tests.** Tests are written before or alongside implementation — never after. A feature is not complete until its tests pass.

- Unit tests cover individual functions and components in isolation
- Integration tests cover the end-to-end flow of each feature
- No code merges to `main` without passing tests
- Tests serve as living documentation of expected behaviour
- Client handoff includes the full test suite so they can verify the system after changes

## Development Methodology

### Phase-Based Approach

**Build → Extract → Reuse → Refine**

1. **Build:** Deliver excellent solution for real client
2. **Extract:** Identify patterns worth preserving
3. **Reuse:** Clone reference for next similar client
4. **Refine:** Sync improvements back to reference

### Extraction Criteria

Not everything becomes "framework." Extract patterns that:

- ✅ Appear in multiple client contexts
- ✅ Solve recurring problems elegantly
- ✅ Are industry-agnostic (or clearly industry-specific)
- ✅ Have been validated in production
- ❌ Are client-specific edge cases
- ❌ Solve one-time problems
- ❌ Are untested or experimental

### Documentation Philosophy

**Code IS documentation, but context matters.**

- Reference implementations show working examples
- Full spec-kit history preserved (educational)
- Decision rationale captured in specs
- Clarification history shows reasoning
- Development log tracks pattern discovery

## Anti-Patterns to Avoid

### ❌ Premature Abstraction
Don't extract reference implementations until patterns proven with N≥2 clients. With only one client, we don't know what's truly reusable.

### ❌ Lowest Common Denominator
Don't dumb down solutions to fit all industries. Better to have excellent industry-specific references than a mediocre generic one.

### ❌ Technical Debt in Reference
Don't sync quick fixes from clients to reference. Only extract clean, maintainable solutions.

### ❌ Forced Reuse
Don't force clients to use reference if their needs differ. Independent codebases mean freedom to diverge.

### ❌ Analysis Paralysis
Don't overthink what's "framework-worthy." Build for client first, extract patterns second.

## Success Metrics

### For Clients
- ✅ Working solution delivered on time and budget
- ✅ Solves actual business problems
- ✅ Maintainable by client or future developers
- ✅ Privacy and cost controls respected

### For Framework
- ✅ Faster time-to-value for second client in same industry
- ✅ Proven patterns documented and reusable
- ✅ Reference implementations that actually get cloned
- ✅ Happy clients who refer others

## Evolution Strategy

FieldKit will evolve through:

1. **Client-Driven Improvements**
   - Real needs trump theoretical benefits
   - Production issues drive better error handling
   - Client feedback shapes priorities

2. **Pattern Recognition**
   - Document similarities across clients
   - Identify generalizable abstractions
   - Build pattern library organically

3. **Selective Reuse**
   - Not everything needs to be reused
   - Shared code only where it adds value
   - Independence over coupling

4. **Industry Expansion**
   - Start with construction (pilot client)
   - Add food service reference next
   - Build library of industry-specific patterns
   - Eventually cover major service verticals

## Long-Term Vision

FieldKit becomes the **go-to framework for custom small business automation**, known for:

- Rapid delivery of tailored solutions
- Deep industry understanding
- Privacy-first design
- Cost-effective AI integration
- Proven, production-ready patterns

Every small service business should have access to sophisticated automation — FieldKit makes that economically viable by encoding expertise in reusable patterns while maintaining customization flexibility.

---

*This philosophy guides all FieldKit development. When in doubt, return to these principles.*
