export type ChatbotStatus = 'draft' | 'active' | 'inactive';

export interface PublicChatbotConfig {
  id: string;
  name: string;
  welcome_message: string | null;
  primary_color: string | null;
}

export type Chatbot = {
  id: string;
  organization_id: string;
  name: string;
  description: string | null;
  website_url: string | null;
  welcome_message: string | null;
  instructions: string | null;
  primary_color: string | null;
  status: ChatbotStatus;
  conversations_count: number;
  leads_count: number;
  last_active: string | null;
  created_at: string;
  updated_at: string;
};

export type ChatbotCreateInput = {
  name: string;
  description?: string | null;
  website_url?: string | null;
  welcome_message?: string | null;
  instructions?: string | null;
  primary_color?: string | null;
  status?: ChatbotStatus;
};

export type ChatbotUpdateInput = {
  name?: string;
  description?: string | null;
  website_url?: string | null;
  welcome_message?: string | null;
  instructions?: string | null;
  primary_color?: string | null;
  status?: ChatbotStatus;
};

export type MockChatbot = {
  id: string;
  name: string;
  description: string;
  status: 'active' | 'draft' | 'inactive';
  conversationsCount: number;
  leadsCount: number;
  lastActive: string;
  websiteUrl?: string;
  welcomeMessage?: string;
  primaryColor?: string;
};

export type Conversation = {
  id: string;
  chatbotId: string;
  userId: string;
  snippet: string;
  createdAt: string;
};

export type Lead = {
  id: string;
  chatbotId: string;
  email: string;
  name?: string;
  createdAt: string;
};

export type OrganizationRole = "owner" | "admin" | "member";

export type Organization = {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
};

export type OrganizationWithRole = {
  id: string;
  name: string;
  role: OrganizationRole;
  created_at: string;
  updated_at: string;
};

// ─── Billing Types ────────────────────────────────────────────────────────────

export interface BillingPeriodOut {
  id: string;
  period_start: string;
  period_end: string;
  plan_tier: string;
  monthly_token_quota: number;
  tokens_used: number;
  tokens_reserved: number;
  tokens_remaining: number;
  is_blocked: boolean;
  blocked_reason?: string | null;
}

export interface BillingSubscriptionOut {
  organization_id: string;
  plan_tier: string;
  status: string;
  current_period_start?: string | null;
  current_period_end?: string | null;
  cancel_at_period_end: boolean;
  canceled_at?: string | null;
  past_due_since?: string | null;
  has_stripe_customer: boolean;
  stripe_enabled: boolean;
  billing_period?: BillingPeriodOut | null;
}

export interface StripeCheckoutSessionOut {
  organization_id: string;
  session_id: string;
  checkout_url: string;
}

export interface StripeBillingPortalOut {
  organization_id: string;
  portal_url: string;
}
