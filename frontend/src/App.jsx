import { Toaster } from "@/components/ui/toaster"
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClientInstance } from '@/lib/query-client'
import { BrowserRouter as Router, Route, Routes } from 'react-router-dom';
import PageNotFound from './lib/PageNotFound';
import { AuthProvider, useAuth } from '@/lib/AuthContext';
import UserNotRegisteredError from '@/components/UserNotRegisteredError';
import Landing from './pages/Landing';
import AuthGate from './components/AuthGate';
import Dashboard from './pages/Dashboard';
import Patterns from './pages/Patterns';
import Compare from './pages/Compare';
import AIAnalyst from './pages/AIAnalyst';
import Connections from './pages/Connections';
import CsvImport from './pages/CsvImport';
import PeriodTracker from './pages/PeriodTracker';
import Explorer from './pages/Explorer';
import Insights from './pages/Insights';
import Records from './pages/Records';
import Report from './pages/Report';
import Settings from './pages/Settings';
import { ViewingProvider } from './lib/ViewingContext';
import PrivacyPolicy from './pages/PrivacyPolicy';
import TermsOfService from './pages/TermsOfService';
import OuraCallback from './pages/OuraCallback';
import FitbitCallback from './pages/FitbitCallback';
import GoogleHealthCallback from './pages/GoogleHealthCallback';

const AuthenticatedApp = () => {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/privacy" element={<PrivacyPolicy />} />
      <Route path="/terms" element={<TermsOfService />} />
      <Route path="/oura-callback" element={<OuraCallback />} />
      <Route path="/fitbit-callback" element={<FitbitCallback />} />
      <Route path="/google-health-callback" element={<GoogleHealthCallback />} />
      <Route element={<AuthGate />}>
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/explorer" element={<Explorer />} />
        <Route path="/patterns" element={<Patterns />} />
        <Route path="/compare" element={<Compare />} />
        <Route path="/analyst" element={<AIAnalyst />} />
        <Route path="/connections" element={<Connections />} />
        <Route path="/import" element={<CsvImport />} />
        <Route path="/period" element={<PeriodTracker />} />
        <Route path="/insights" element={<Insights />} />
        <Route path="/records" element={<Records />} />
        <Route path="/report" element={<Report />} />
        <Route path="/settings" element={<Settings />} />
      </Route>
      <Route path="*" element={<PageNotFound />} />
    </Routes>
  );
};


function App() {

  return (
    <AuthProvider>
      <QueryClientProvider client={queryClientInstance}>
        <ViewingProvider>
          <Router>
            <AuthenticatedApp />
          </Router>
        </ViewingProvider>
        <Toaster />
      </QueryClientProvider>
    </AuthProvider>
  )
}

export default App