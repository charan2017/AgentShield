import { useEffect, useState } from "react";

const API_URL = "http://127.0.0.1:8000";

function SecurityOverview() {
  const [ledger, setLedger] = useState([]);
  const [approvals, setApprovals] = useState([]);

  async function loadOverview() {
    try {
      const [ledgerResponse, approvalResponse] =
        await Promise.all([
          fetch(`${API_URL}/ledger`),
          fetch(`${API_URL}/approvals/pending`),
        ]);

      if (ledgerResponse.ok) {
        const ledgerData =
          await ledgerResponse.json();

        setLedger(
          ledgerData.events || []
        );
      }

      if (approvalResponse.ok) {
        const approvalData =
          await approvalResponse.json();

        setApprovals(
          approvalData.requests || []
        );
      }
    } catch (error) {
      console.error(
        "Overview loading failed:",
        error
      );
    }
  }

  useEffect(() => {
    loadOverview();

    const interval = setInterval(
      loadOverview,
      3000
    );

    return () => {
      clearInterval(interval);
    };
  }, []);

  const decisionEvents =
    ledger.filter(
      (event) =>
        event.event_type ===
        "SECURITY_DECISION"
    );

  const allowedCount =
    decisionEvents.filter(
      (event) =>
        event.metadata?.decision ===
        "ALLOW"
    ).length;

  const reviewCount =
    decisionEvents.filter(
      (event) =>
        event.metadata?.decision ===
        "REVIEW"
    ).length;

  const blockedCount =
    decisionEvents.filter(
      (event) =>
        event.metadata?.decision ===
        "BLOCK"
    ).length;

  return (
    <section className="overview-grid">

      <div className="overview-stat">

        <div className="overview-stat-label">
          ALLOWED
        </div>

        <div className="overview-stat-value allow-text">
          {allowedCount}
        </div>

        <div className="overview-stat-description">
          Approved security decisions
        </div>

      </div>


      <div className="overview-stat">

        <div className="overview-stat-label">
          REVIEWS
        </div>

        <div className="overview-stat-value review-text">
          {reviewCount}
        </div>

        <div className="overview-stat-description">
          Decisions requiring humans
        </div>

      </div>


      <div className="overview-stat">

        <div className="overview-stat-label">
          BLOCKED
        </div>

        <div className="overview-stat-value block-text">
          {blockedCount}
        </div>

        <div className="overview-stat-description">
          Security violations stopped
        </div>

      </div>


      <div className="overview-stat">

        <div className="overview-stat-label">
          PENDING APPROVALS
        </div>

        <div className="overview-stat-value">
          {approvals.length}
        </div>

        <div className="overview-stat-description">
          Waiting for human action
        </div>

      </div>


      <div className="overview-agent">

        <div>

          <div className="overview-stat-label">
            ACTIVE AGENT
          </div>

          <div className="overview-agent-name">
            AGENT-001
          </div>

        </div>


        <div className="overview-agent-status">

          <span className="status-dot"></span>

          MONITORED

        </div>

      </div>


      <div className="overview-policy">

        <div className="overview-stat-label">
          ACTIVE POLICY
        </div>

        <div className="overview-policy-grid">

          <span>
            Transaction
          </span>

          <strong>
            ₹5,000
          </strong>


          <span>
            Daily limit
          </span>

          <strong>
            ₹15,000
          </strong>


          <span>
            Human review
          </span>

          <strong>
            Above ₹3,000
          </strong>

        </div>

      </div>

    </section>
  );
}

export default SecurityOverview;