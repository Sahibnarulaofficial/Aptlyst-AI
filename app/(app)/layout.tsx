import { Sidebar } from '@/components/layout/sidebar';
import { MobileNav } from '@/components/layout/mobile-nav';

/**
 * Layout for authenticated app pages — includes sidebar and mobile nav.
 * All routes in this group require a valid Supabase session
 * (enforced by middleware.ts before this layout renders).
 */
export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-col md:flex-row h-screen overflow-hidden">
      <Sidebar />
      <MobileNav />
      <main className="flex-1 overflow-y-auto bg-slate-900 md:border-l md:border-slate-800">
        {children}
      </main>
    </div>
  );
}
