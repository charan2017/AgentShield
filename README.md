# AgentShield

## AI Agent Payment Security Gateway

AgentShield is a security layer between an AI payment agent and payment execution.

Instead of allowing an AI agent to directly execute a financial transaction, every payment request is converted into a structured intent and independently evaluated by AgentShield before any payment order can be created.

```text
User
  ↓
AI Payment Agent
  ↓
Natural-language Command Parser
  ↓
Structured Payment Intent
  ↓
AgentShield Security Engine
  ├── Intent / Authorization Validation
  ├── Policy Enforcement
  ├── Risk Detection
  ├── Behavior Monitoring
  ├── Spending Limits
  ├── Idempotency Checks
  └── Human Approval
  ↓
ALLOW / REVIEW / BLOCK
  ├── ALLOW  → Razorpay Checkout
  ├── REVIEW → Human Approval → Razorpay
  └── BLOCK  → Payment Rejected
Why AgentShield?

AI agents can automate financial actions, but an AI-generated payment request should not automatically be trusted.

AgentShield acts as an independent security gateway between the AI agent and payment execution.

The AI agent can request a payment, but AgentShield independently evaluates the request and determines whether it should be:

ALLOW — payment can proceed
REVIEW — human approval is required
BLOCK — payment is rejected
Core Features
AI-powered natural-language payment requests
Intent and authorization validation
Policy enforcement
Risk scoring
Behavior monitoring
Server-side transaction limits
Daily spending limits
Human approval workflow
Razorpay test-mode integration
Scheduled payments
Threat simulation
Duplicate-request protection
Persistent payment ledger
Action audit ledger
Configurable security settings
AI Payment Agent

Users can enter commands such as:

pay 3000 to Rahul
pay 7000 to Rahul tomorrow
pay 2500 to Rahul on 15th
remind me to pay 1200 to electricity monthly

The command parser converts natural-language requests into structured payment information such as:

payment type
amount
recipient
schedule
intent

The structured request is then passed through the AgentShield security engine.

Security Decision Engine

AgentShield evaluates every payment before payment execution.

ALLOW

The request satisfies the configured security controls.

Payment Request
      ↓
Security Evaluation
      ↓
ALLOW
      ↓
Razorpay Checkout
REVIEW

Sensitive transactions are sent for human approval.

Payment Request
      ↓
Security Evaluation
      ↓
REVIEW
      ↓
Approval Center
      ↓
Approve
      ↓
Razorpay Checkout
BLOCK

Unsafe requests are rejected and no payment order should be created.

Payment Request
      ↓
Security Evaluation
      ↓
BLOCK
      ↓
Payment Rejected
Authorization Protection

Authorization mismatches are treated as hard security failures.

Example:

Requested Amount:  ₹5,000
Authorized Amount: ₹1,000

Result:
BLOCK

The request should not be converted into a human approval request because the requested payment does not match the authorized intent.

Risk Engine

The risk engine evaluates signals such as:

transaction amount
merchant familiarity
unusual spending behavior
high-risk payment categories

The result contributes to AgentShield's overall security decision.

Human Approval Center

The Approval Center allows sensitive requests to be reviewed by a human before payment execution.

Approval flow:

AI Request
    ↓
AgentShield
    ↓
REVIEW
    ↓
Human Approval
    ↓
Payment Execution

Rejected requests cannot proceed to payment execution.

Scheduled Payments

AgentShield supports scheduled payment requests.

Example:

pay 100 to Rahul tomorrow

The scheduler stores the request and processes it when it becomes due.

A scheduled request is re-evaluated by the security layer before execution rather than blindly trusting the original decision.

Threat Lab

The Threat Lab demonstrates adversarial agent behavior.

Current simulations include:

Amount Manipulation
Recipient Manipulation
Duplicate Payment
Payment Loop

Example:

Authorized Amount: ₹2,000
Requested Amount:  ₹20,000

Result:
BLOCK

This demonstrates that an AI agent cannot simply modify payment parameters and bypass the security layer.
Action Audit Ledger

AgentShield maintains a persistent audit trail of important actions.

Examples include:

payment requests
security decisions
approvals
payment-order creation
payment verification
scheduled execution
security setting changes

This allows the system to show what the agent requested and how AgentShield responded.

Security Settings

The application provides configurable security controls:

Maximum Transaction Amount
Daily Spending Limit
Human Approval Threshold

Security policies are enforced by the backend.

The frontend is not treated as the final security authority.
Architecture
Backend
backend/
├── main.py
└── services/
    ├── action_ledger.py
    ├── approval_service.py
    ├── behavior_engine.py
    ├── command_parser.py
    ├── database.py
    ├── idempotency.py
    ├── payment_ledger.py
    ├── policy_engine.py
    ├── razorpay_service.py
    ├── risk_engine.py
    ├── scheduler_service.py
    ├── security_config.py
    └── subscription_service.py
Frontend
frontend/
├── src/
│   ├── App.jsx
│   ├── App.css
│   ├── index.css
│   ├── main.jsx
│   ├── SecurityOverview.jsx
│   └── ThreatSimulator.jsx
├── package.json
└── vite.config.js
Application Pages

The interface contains security and payment management sections including:

Dashboard
AI Payment Agent
Payments
Scheduled Payments
Recurring Bills
Approval Center
Threat Lab
Action Ledger
Agent Monitoring
Settings
Technology Stack
Backend
Python
FastAPI
Uvicorn
Pydantic
SQLite
python-dotenv
Razorpay Python SDK
Frontend
React
Vite
JavaScript / JSX
Framer Motion
Lucide React
Recharts
clsx
Payment Gateway
Razorpay Test Mode