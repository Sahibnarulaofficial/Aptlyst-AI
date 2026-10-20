'use client';

import { useState } from 'react';
import Link from 'next/link';
import { Bot, Loader2 } from 'lucide-react';
import { createClient } from '@/lib/supabase/client';

export default function SignupPage() {


  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [emailSent, setEmailSent] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);

    const supabase = createClient();
    const { error: signUpError } = await supabase.auth.signUp({
      email,
      password,
      options: {
        emailRedirectTo: `${window.location.origin}/auth/callback`,
      },
    });

    if (signUpError) {
      setError(signUpError.message);
      setLoading(false);
      return;
    }

    setEmailSent(true);
    setLoading(false);
  }

  if (emailSent) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center px-4">
        <div className="w-full max-w-sm space-y-6 text-center">
          <div className="flex justify-center">
            <div className="h-10 w-10 rounded-xl bg-blue-600 flex items-center justify-center">
              <Bot className="h-6 w-6 text-white" />
            </div>
          </div>
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-3">
            <h2 className="text-lg font-semibold text-slate-100">Check your email</h2>
            <p className="text-sm text-slate-400">
              We sent a confirmation link to{' '}
              <span className="text-slate-200 font-medium">{email}</span>.
              Click the link to verify your account.
            </p>
          </div>
          <p className="text-sm text-slate-500">
            Already confirmed?{' '}
            <Link href="/login" className="text-blue-400 hover:text-blue-300 transition-colors">
              Sign in
            </Link>
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-950 flex items-center justify-center px-4">
      <div className="w-full max-w-sm space-y-8">
        {/* Logo */}
        <div className="flex flex-col items-center gap-3">
          <div className="h-10 w-10 rounded-xl bg-blue-600 flex items-center justify-center">
            <Bot className="h-6 w-6 text-white" />
          </div>
          <div className="text-center">
            <h1 className="text-2xl font-bold text-slate-100 tracking-tight">
              Aptlyst AI
            </h1>
            <p className="text-slate-400 text-sm mt-1">Create your account</p>
          </div>
        </div>

        {/* Form */}
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-4">
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <label htmlFor="email" className="text-sm font-medium text-slate-300">
                Email
              </label>
              <input
                id="email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full px-3 py-2 bg-slate-950 border border-slate-700 rounded-lg text-slate-100 placeholder-slate-500 text-sm focus:outline-none focus:ring-2 focus:ring-blue-600 focus:border-transparent transition"
                placeholder="you@example.com"
              />
            </div>

            <div className="space-y-1.5">
              <label htmlFor="password" className="text-sm font-medium text-slate-300">
                Password
              </label>
              <input
                id="password"
                type="password"
                autoComplete="new-password"
                required
                minLength={6}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full px-3 py-2 bg-slate-950 border border-slate-700 rounded-lg text-slate-100 placeholder-slate-500 text-sm focus:outline-none focus:ring-2 focus:ring-blue-600 focus:border-transparent transition"
                placeholder="Min. 6 characters"
              />
            </div>

            {error && (
              <p className="text-sm text-red-400 bg-red-950/30 border border-red-900/40 rounded-lg px-3 py-2">
                {error}
              </p>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-60 disabled:cursor-not-allowed text-white px-4 py-2.5 rounded-lg font-medium text-sm transition-colors"
            >
              {loading && <Loader2 className="h-4 w-4 animate-spin" />}
              {loading ? 'Creating account…' : 'Create account'}
            </button>
          </form>
        </div>

        <p className="text-center text-sm text-slate-500">
          Already have an account?{' '}
          <Link href="/login" className="text-blue-400 hover:text-blue-300 transition-colors">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
