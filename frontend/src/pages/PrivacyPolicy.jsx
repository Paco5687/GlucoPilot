import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";

export default function PrivacyPolicy() {
  return (
    <div className="min-h-screen bg-background">
      <div className="max-w-3xl mx-auto px-4 py-12">
        <Link to="/" className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground mb-8">
          <ArrowLeft className="w-4 h-4" /> Back to Home
        </Link>

        <h1 className="text-3xl font-bold mb-2">Privacy Policy</h1>
        <p className="text-sm text-muted-foreground mb-8">Last updated: May 3, 2026</p>

        <div className="prose prose-sm max-w-none space-y-6 text-foreground">
          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">1. Introduction</h2>
            <p>
              GlucoPilot ("we," "us," or "our") operates the GlucoPilot web application (the "Service"). This Privacy Policy explains how we collect, use, disclose, and safeguard your information when you use our Service.
            </p>
            <p>
              GlucoPilot processes health-related data, including continuous glucose monitor (CGM) readings, insulin dosing records, carbohydrate intake logs, and related health metrics. We treat all such data as Protected Health Information (PHI) and apply appropriate safeguards consistent with the principles of the Health Insurance Portability and Accountability Act (HIPAA) and applicable data protection regulations.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">2. Information We Collect</h2>
            <h3 className="text-base font-medium mt-4 mb-2">2.1 Information You Provide</h3>
            <ul className="list-disc pl-6 space-y-1">
              <li><strong>Account Information:</strong> Email address, name, and authentication credentials.</li>
              <li><strong>Health Data:</strong> Glucose readings, insulin doses, carbohydrate intake, blood glucose checks, temporary basal rates, and related treatment data imported from Nightscout, Dexcom Clarity, Glooko, or CSV files.</li>
              <li><strong>Profile Settings:</strong> Nightscout URL and API credentials, display preferences, and notification settings.</li>
              <li><strong>Period/Cycle Data:</strong> Menstrual cycle logs including phase, flow, and symptom information (if you choose to use this feature).</li>
              <li><strong>Communications:</strong> Messages and queries submitted to the AI Analyst feature.</li>
            </ul>

            <h3 className="text-base font-medium mt-4 mb-2">2.2 Information Collected Automatically</h3>
            <ul className="list-disc pl-6 space-y-1">
              <li><strong>Usage Data:</strong> Pages visited, features used, and interaction patterns within the Service.</li>
              <li><strong>Device Information:</strong> Browser type, operating system, and screen resolution.</li>
              <li><strong>Log Data:</strong> Access timestamps, error logs, and session duration.</li>
            </ul>

            <h3 className="text-base font-medium mt-4 mb-2">2.3 Information from Third Parties</h3>
            <ul className="list-disc pl-6 space-y-1">
              <li><strong>Nightscout:</strong> CGM readings, treatments, and profile data synchronized from your Nightscout instance.</li>
              <li><strong>CSV Imports:</strong> Health data from exported files (Glooko, Dexcom Clarity, or similar platforms).</li>
            </ul>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">3. How We Use Your Information</h2>
            <p>We use the information we collect to:</p>
            <ul className="list-disc pl-6 space-y-1">
              <li>Provide, operate, and maintain the Service, including glucose trend visualization, pattern detection, and statistical analysis.</li>
              <li>Generate AI-powered insights and analysis of your glucose data using third-party large language model (LLM) services.</li>
              <li>Detect and display glucose patterns and trends.</li>
              <li>Enable data sharing with healthcare providers or other individuals you authorize.</li>
              <li>Send you service-related communications (e.g., sync status, alerts).</li>
              <li>Improve and optimize the Service.</li>
              <li>Comply with legal obligations.</li>
            </ul>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">4. AI-Powered Features</h2>
            <p>
              The AI Analyst and pattern detection features transmit aggregated, de-identified glucose statistics and conversation content to third-party AI service providers for processing. Specifically:
            </p>
            <ul className="list-disc pl-6 space-y-1">
              <li>Statistical summaries (averages, time-in-range percentages, standard deviations) are sent — not raw individual readings.</li>
              <li>AI conversations are stored in your account and are accessible only by you.</li>
              <li>We do not use your health data to train AI models.</li>
              <li>AI-generated insights are for informational purposes only and do not constitute medical advice.</li>
            </ul>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">5. Data Sharing and Disclosure</h2>
            <p>We do not sell your personal information or health data. We may share your information only in the following circumstances:</p>
            <ul className="list-disc pl-6 space-y-1">
              <li><strong>With Your Consent:</strong> When you explicitly share your data with healthcare providers or other individuals via the Sharing feature.</li>
              <li><strong>Service Providers:</strong> With third-party services that help us operate the Service (hosting, AI processing), bound by data processing agreements and confidentiality obligations.</li>
              <li><strong>Legal Requirements:</strong> When required by law, regulation, legal process, or governmental request.</li>
              <li><strong>Safety:</strong> When we believe disclosure is necessary to protect the rights, safety, or property of our users or the public.</li>
            </ul>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">6. Data Security</h2>
            <p>We implement appropriate technical and organizational measures to protect your health data, including:</p>
            <ul className="list-disc pl-6 space-y-1">
              <li>Encryption of data in transit (TLS/SSL) and at rest.</li>
              <li>Authentication and role-based access controls.</li>
              <li>Session timeout and automatic logout for healthcare provider accounts.</li>
              <li>Audit logging of data access events.</li>
              <li>Consent verification before initial data access.</li>
            </ul>
            <p>
              While we strive to use commercially acceptable means to protect your data, no method of electronic storage or transmission is 100% secure, and we cannot guarantee absolute security.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">7. Data Retention</h2>
            <p>
              We retain your health data for as long as your account is active or as needed to provide the Service. You may request deletion of your account and associated data at any time by contacting us. Upon account deletion, we will remove your data within 30 days, except where retention is required by law.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">8. Your Rights</h2>
            <p>Depending on your jurisdiction, you may have the following rights regarding your personal data:</p>
            <ul className="list-disc pl-6 space-y-1">
              <li><strong>Access:</strong> Request a copy of the personal data we hold about you.</li>
              <li><strong>Correction:</strong> Request correction of inaccurate or incomplete data.</li>
              <li><strong>Deletion:</strong> Request deletion of your personal data.</li>
              <li><strong>Portability:</strong> Request a machine-readable copy of your data.</li>
              <li><strong>Restriction:</strong> Request that we limit the processing of your data.</li>
              <li><strong>Objection:</strong> Object to the processing of your data for certain purposes.</li>
              <li><strong>Withdraw Consent:</strong> Withdraw consent for data processing at any time.</li>
            </ul>
            <p>
              To exercise any of these rights, please contact us using the information provided in Section 12.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">9. Children's Privacy</h2>
            <p>
              The Service is not intended for use by individuals under the age of 13 (or the applicable age of digital consent in your jurisdiction). We do not knowingly collect personal information from children. If a parent or guardian manages a child's diabetes data, they are responsible for providing appropriate consent and overseeing the child's use of the Service.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">10. International Data Transfers</h2>
            <p>
              Your data may be transferred to and processed in countries other than your country of residence. We ensure appropriate safeguards are in place for such transfers in compliance with applicable data protection laws.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">11. Changes to This Policy</h2>
            <p>
              We may update this Privacy Policy from time to time. We will notify you of any material changes by posting the new Privacy Policy on this page and updating the "Last updated" date. Your continued use of the Service after such modifications constitutes your acknowledgment of the revised policy.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">12. Contact Us</h2>
            <p>
              If you have questions or concerns about this Privacy Policy or our data practices, please contact us at:
            </p>
            <p className="mt-2">
              <strong>Email:</strong> privacy@glucopilot.com
            </p>
          </section>
        </div>
      </div>
    </div>
  );
}