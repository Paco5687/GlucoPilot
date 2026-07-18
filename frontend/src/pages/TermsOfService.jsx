import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";

export default function TermsOfService() {
  return (
    <div className="min-h-screen bg-background">
      <div className="max-w-3xl mx-auto px-4 py-12">
        <Link to="/" className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground mb-8">
          <ArrowLeft className="w-4 h-4" /> Back to Home
        </Link>

        <h1 className="text-3xl font-bold mb-2">Terms of Service</h1>
        <p className="text-sm text-muted-foreground mb-8">Last updated: May 3, 2026</p>

        <div className="prose prose-sm max-w-none space-y-6 text-foreground">
          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">1. Acceptance of Terms</h2>
            <p>
              By accessing or using the GlucoPilot web application (the "Service"), you agree to be bound by these Terms of Service ("Terms"). If you do not agree to these Terms, you must not access or use the Service. These Terms constitute a legally binding agreement between you and GlucoPilot ("we," "us," or "our").
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">2. Description of Service</h2>
            <p>
              GlucoPilot is a web-based platform that allows users to visualize, analyze, and gain insights from continuous glucose monitor (CGM) data, insulin dosing records, carbohydrate intake logs, and related health metrics. The Service includes:
            </p>
            <ul className="list-disc pl-6 space-y-1">
              <li>Dashboard visualization of glucose trends, patterns, and statistics.</li>
              <li>Integration with Nightscout instances and CSV data imports from platforms such as Glooko and Dexcom Clarity.</li>
              <li>AI-powered glucose data analysis and conversational insights.</li>
              <li>Pattern detection and comparison tools.</li>
              <li>Data sharing with healthcare providers or authorized individuals.</li>
              <li>Menstrual cycle tracking and correlation analysis.</li>
            </ul>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">3. Medical Disclaimer</h2>
            <div className="bg-destructive/10 border border-destructive/20 rounded-lg p-4">
              <p className="font-semibold text-destructive mb-2">IMPORTANT — PLEASE READ CAREFULLY</p>
              <p>
                GlucoPilot is an <strong>educational and informational tool only</strong>. The Service is NOT a medical device, is NOT FDA-cleared or approved, and is NOT intended to diagnose, treat, cure, or prevent any disease or medical condition.
              </p>
              <ul className="list-disc pl-6 space-y-2 mt-3">
                <li>The Service does <strong>not</strong> provide medical advice, insulin dosing recommendations, or treatment guidance.</li>
                <li>AI-generated insights are produced by artificial intelligence algorithms and are <strong>not</strong> reviewed by medical professionals. They may contain errors, inaccuracies, or misleading information.</li>
                <li>You must <strong>never</strong> make changes to your medication, insulin dosing, diet, or treatment plan based solely on information from GlucoPilot.</li>
                <li>Always consult your physician, endocrinologist, certified diabetes educator, or other qualified healthcare provider before making any healthcare decisions.</li>
                <li>In case of a medical emergency, call your local emergency services immediately. Do not rely on GlucoPilot for emergency medical decisions.</li>
                <li>GlucoPilot does <strong>not</strong> replace your CGM device, insulin pump, or any FDA-cleared medical device or system.</li>
              </ul>
            </div>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">4. Eligibility</h2>
            <p>
              You must be at least 13 years of age (or the applicable age of digital consent in your jurisdiction) to use the Service. If you are under 18, you must have the consent and supervision of a parent or legal guardian. By using the Service, you represent and warrant that you meet these requirements.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">5. Account Registration and Security</h2>
            <ul className="list-disc pl-6 space-y-1">
              <li>You must provide accurate and complete information during registration.</li>
              <li>You are responsible for maintaining the confidentiality of your account credentials.</li>
              <li>You are responsible for all activities that occur under your account.</li>
              <li>You must notify us immediately of any unauthorized use of your account.</li>
              <li>We reserve the right to suspend or terminate accounts that violate these Terms.</li>
            </ul>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">6. User Data and Ownership</h2>
            <p>
              You retain full ownership of all health data you upload, import, or enter into the Service ("User Data"). By using the Service, you grant us a limited, non-exclusive license to process, store, display, and analyze your User Data solely for the purpose of providing and improving the Service.
            </p>
            <p>
              You are responsible for ensuring that you have the right to upload and share any data you provide to the Service, including data from third-party platforms like Nightscout.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">7. Data Sharing</h2>
            <p>
              The Service allows you to share your health data with other individuals, including healthcare providers. When you share your data:
            </p>
            <ul className="list-disc pl-6 space-y-1">
              <li>You are solely responsible for deciding who to share your data with.</li>
              <li>Shared data is provided on a read-only basis unless otherwise specified.</li>
              <li>You may revoke access at any time through the Sharing settings.</li>
              <li>We are not responsible for how recipients use data you choose to share.</li>
              <li>Healthcare providers using the Service are subject to their own professional obligations regarding patient data.</li>
            </ul>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">8. Acceptable Use</h2>
            <p>You agree not to:</p>
            <ul className="list-disc pl-6 space-y-1">
              <li>Use the Service for any unlawful purpose or in violation of any applicable laws.</li>
              <li>Attempt to gain unauthorized access to any portion of the Service or any other systems or networks connected to the Service.</li>
              <li>Interfere with or disrupt the integrity or performance of the Service.</li>
              <li>Upload malicious code, viruses, or any harmful data.</li>
              <li>Impersonate any person or entity, or misrepresent your affiliation with a person or entity.</li>
              <li>Use the Service to store or transmit data that infringes on any third party's rights.</li>
              <li>Access another user's data without their explicit authorization through the Service's sharing features.</li>
              <li>Use automated means (bots, scrapers) to access the Service without our written consent.</li>
            </ul>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">9. Third-Party Integrations</h2>
            <p>
              The Service integrates with third-party platforms and services, including but not limited to Nightscout, Glooko, and Dexcom Clarity. Your use of these third-party services is subject to their respective terms of service and privacy policies. We are not responsible for the availability, accuracy, or security of third-party services.
            </p>
            <p>
              We use third-party AI service providers to power the AI Analyst and pattern detection features. While we take measures to protect your data, you acknowledge that aggregated health statistics are transmitted to these providers for processing.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">10. Data Accuracy and Reliability</h2>
            <p>
              While we strive for accuracy, we do not guarantee that the data displayed by the Service is error-free. Glucose readings, statistics, and calculations may contain inaccuracies due to:
            </p>
            <ul className="list-disc pl-6 space-y-1">
              <li>Data transmission errors from CGM devices or third-party platforms.</li>
              <li>Gaps or duplications in imported data.</li>
              <li>Rounding, interpolation, or aggregation in statistical calculations.</li>
              <li>Limitations of AI-generated analysis.</li>
            </ul>
            <p>
              Always verify critical health data against your CGM device, blood glucose meter, or medical records.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">11. Limitation of Liability</h2>
            <div className="bg-muted rounded-lg p-4">
              <p>
                TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, GLUCOPILOT AND ITS OFFICERS, DIRECTORS, EMPLOYEES, AGENTS, AND AFFILIATES SHALL NOT BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES, INCLUDING BUT NOT LIMITED TO:
              </p>
              <ul className="list-disc pl-6 space-y-1 mt-2">
                <li>PERSONAL INJURY OR ADVERSE HEALTH OUTCOMES ARISING FROM YOUR USE OF OR RELIANCE ON THE SERVICE.</li>
                <li>LOSS OF DATA, REVENUE, OR PROFITS.</li>
                <li>ERRORS, INACCURACIES, OR OMISSIONS IN DATA DISPLAYED BY THE SERVICE.</li>
                <li>ANY DECISIONS MADE BASED ON INFORMATION PROVIDED BY THE SERVICE, INCLUDING AI-GENERATED INSIGHTS.</li>
                <li>INTERRUPTION OR UNAVAILABILITY OF THE SERVICE.</li>
              </ul>
              <p className="mt-2">
                IN NO EVENT SHALL OUR TOTAL LIABILITY EXCEED THE AMOUNT YOU PAID TO US, IF ANY, FOR USE OF THE SERVICE DURING THE TWELVE (12) MONTHS PRECEDING THE CLAIM.
              </p>
            </div>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">12. Disclaimer of Warranties</h2>
            <p>
              THE SERVICE IS PROVIDED "AS IS" AND "AS AVAILABLE" WITHOUT WARRANTIES OF ANY KIND, WHETHER EXPRESS, IMPLIED, OR STATUTORY, INCLUDING BUT NOT LIMITED TO IMPLIED WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, ACCURACY, AND NON-INFRINGEMENT. WE DO NOT WARRANT THAT THE SERVICE WILL BE UNINTERRUPTED, ERROR-FREE, SECURE, OR FREE OF VIRUSES OR OTHER HARMFUL COMPONENTS.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">13. Indemnification</h2>
            <p>
              You agree to indemnify, defend, and hold harmless GlucoPilot and its officers, directors, employees, and agents from and against any claims, liabilities, damages, losses, and expenses (including reasonable attorneys' fees) arising out of or in any way connected with your access to or use of the Service, your violation of these Terms, or your violation of any rights of another party.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">14. Termination</h2>
            <p>
              We may suspend or terminate your access to the Service at any time, with or without cause, and with or without notice. You may stop using the Service at any time. Upon termination, your right to use the Service will immediately cease. Sections 3, 6, 11, 12, 13, and 16 shall survive termination.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">15. Changes to Terms</h2>
            <p>
              We reserve the right to modify these Terms at any time. We will provide notice of material changes by posting the updated Terms on this page and updating the "Last updated" date. Your continued use of the Service after such modifications constitutes acceptance of the revised Terms. If you do not agree to the new Terms, you must stop using the Service.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">16. Governing Law and Dispute Resolution</h2>
            <p>
              These Terms shall be governed by and construed in accordance with the laws of the State of Delaware, United States, without regard to its conflict of law provisions. Any dispute arising from or relating to these Terms or the Service shall be resolved through binding arbitration in accordance with the rules of the American Arbitration Association, except that either party may seek injunctive relief in any court of competent jurisdiction.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">17. Severability</h2>
            <p>
              If any provision of these Terms is held to be invalid or unenforceable, the remaining provisions shall continue in full force and effect. The invalid or unenforceable provision shall be modified to the minimum extent necessary to make it valid and enforceable.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">18. Entire Agreement</h2>
            <p>
              These Terms, together with the <Link to="/privacy" className="text-primary underline">Privacy Policy</Link>, constitute the entire agreement between you and GlucoPilot regarding your use of the Service, and supersede all prior agreements, understandings, and communications, whether written or oral.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold mt-8 mb-3">19. Contact Us</h2>
            <p>
              If you have questions about these Terms, please contact us at:
            </p>
            <p className="mt-2">
              <strong>Email:</strong> legal@glucopilot.com
            </p>
          </section>
        </div>
      </div>
    </div>
  );
}