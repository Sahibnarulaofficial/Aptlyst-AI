export type Chatbot = {
  id: string;
  name: string;
  description: string;
  status: "active" | "draft" | "inactive";
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

