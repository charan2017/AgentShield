import { useState } from "react";

const API_URL = "http://127.0.0.1:8000";

function ThreatSimulator({ onResult }) {
  const [running, setRunning] = useState(false);
  const [activeAttack, setActiveAttack] =
    useState("");

  const [error, setError] = useState("");

  async function sendAttack(payload) {
    try {
      const response = await fetch(
        `${API_URL}/agent/payment-request`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        }
      );

      const data = await response.json();

      if (!response.ok) {
        throw new Error(
          data.detail ||
            "AgentShield rejected the simulator request."
        );
      }

      if (onResult) {
        onResult(data);
      }

      return data;
    } catch (err) {
      setError(
        err.message ||
          "Unable to reach AgentShield."
      );

      return null;
    }
  }

  async function runAttack(type) {
    if (running) {
      return;
    }

    setRunning(true);
    setActiveAttack(type);
    setError("");

    const sessionId =
      `ATTACK-${Date.now()}`;

    const baseRequest = {
      intent_id:
        `ATTACK-${Date.now()}`,

      agent_id:
        "AGENT-ATTACK-SIM",

      session_id:
        sessionId,

      recipient:
        "Rahul",

      amount:
        2000,

      currency:
        "INR",

      intended_amount:
        2000,

      intended_recipient:
        "Rahul",

      max_transaction_amount:
        5000,

      daily_limit:
        15000,

      amount_spent_today:
        0,

      previous_payment_same_request:
        false,

      merchant:
        "Rahul",

      category:
        "personal_transfer",

      merchant_known:
        true,

      unusual:
        true,
    };

    // ========================================================
    // AMOUNT MANIPULATION
    // ========================================================

    if (type === "amount") {
      await sendAttack({
        ...baseRequest,

        intent_id:
          `ATTACK-AMOUNT-${Date.now()}`,

        amount:
          20000,

        intended_amount:
          2000,
      });
    }

    // ========================================================
    // RECIPIENT MANIPULATION
    // ========================================================

    if (type === "recipient") {
      await sendAttack({
        ...baseRequest,

        intent_id:
          `ATTACK-RECIPIENT-${Date.now()}`,

        recipient:
          "Unknown Recipient",

        intended_recipient:
          "Rahul",
      });
    }

    // ========================================================
    // DUPLICATE PAYMENT
    // ========================================================

    if (type === "duplicate") {
      await sendAttack({
        ...baseRequest,

        intent_id:
          `ATTACK-DUPLICATE-${Date.now()}`,

        previous_payment_same_request:
          true,
      });
    }

    // ========================================================
    // PAYMENT LOOP
    // ========================================================

    if (type === "loop") {
      let latest = null;

      for (let i = 1; i <= 4; i++) {
        latest =
          await sendAttack({
            ...baseRequest,

            intent_id:
              `ATTACK-LOOP-${i}-${Date.now()}`,

            session_id:
              sessionId,

            unusual:
              i >= 3,
          });

        await new Promise(
          (resolve) =>
            setTimeout(resolve, 500)
        );
      }

      if (latest && onResult) {
        onResult(latest);
      }
    }

    setRunning(false);
    setActiveAttack("");
  }

  return (
    <section className="card threat-card">

      <div className="section-label">
        AGENT THREAT SIMULATOR
      </div>

      <div className="threat-heading">

        <div>

          <h2>
            Test AgentShield defenses
          </h2>

          <p className="description">
            Simulate common ways a malicious or
            compromised AI agent could attempt
            to manipulate a payment.
          </p>

        </div>

        <div className="threat-badge">
          LIVE SECURITY TEST
        </div>

      </div>


      <div className="threat-grid">

        <button
          className="threat-button"
          disabled={running}
          onClick={() =>
            runAttack("amount")
          }
        >

          <span className="threat-icon">
            💰
          </span>

          <span className="threat-text">

            <strong>
              Amount Manipulation
            </strong>

            <small>
              User: ₹2,000
              {" → "}
              Agent: ₹20,000
            </small>

          </span>

        </button>


        <button
          className="threat-button"
          disabled={running}
          onClick={() =>
            runAttack("recipient")
          }
        >

          <span className="threat-icon">
            👤
          </span>

          <span className="threat-text">

            <strong>
              Recipient Manipulation
            </strong>

            <small>
              Rahul
              {" → "}
              Unknown Recipient
            </small>

          </span>

        </button>


        <button
          className="threat-button"
          disabled={running}
          onClick={() =>
            runAttack("duplicate")
          }
        >

          <span className="threat-icon">
            🔁
          </span>

          <span className="threat-text">

            <strong>
              Duplicate Payment
            </strong>

            <small>
              Same transaction submitted again
            </small>

          </span>

        </button>


        <button
          className="threat-button"
          disabled={running}
          onClick={() =>
            runAttack("loop")
          }
        >

          <span className="threat-icon">
            ♻️
          </span>

          <span className="threat-text">

            <strong>
              Agent Payment Loop
            </strong>

            <small>
              Repeated attempts in one session
            </small>

          </span>

        </button>

      </div>


      {running && (

        <div className="threat-running">

          <span className="threat-spinner"></span>

          Running{" "}
          <strong>
            {activeAttack}
          </strong>
          {" "}attack simulation...

        </div>

      )}


      {error && (

        <div className="error-box">

          <strong>
            Simulator Error
          </strong>

          <div>
            {error}
          </div>

        </div>

      )}

    </section>
  );
}

export default ThreatSimulator;