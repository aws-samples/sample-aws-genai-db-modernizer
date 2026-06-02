import Box from "@cloudscape-design/components/box";

export const LayoutConfigurations =
{
      "application-title": "Database Modernizer"

};

export const SideNavigationConfigurations = {
  header: {
    href: "/",
    text: "Database Modernizer",
  },
  items: [
    {
      href: "/",
      text: "Home",
      type: "link",
    },
    {
      href: "/dashboard",
      text: "Dashboard",
      type: "link",
    },
    {
      href: "/analysis/create",
      text: "New analysis",
      type: "link",
    },
    {
      href: "/analysis/local",
      text: "Local analysis",
      type: "link",
    },
    {
      href: "/settings/s",
      text: "Settings",
      type: "link",
    },
    {
      type: "divider"
    },
    {
      href: "/debug",
      text: "API Debug",
      type: "link",
    },
  ],
};

/**
 * API Configuration
 * Uses relative URLs since UI and API share the same ALB.
 */
export const ApiConfigurations = {
  baseUrl: process.env.REACT_APP_API_URL || '/api/v1/',
  timeout: 30000, // 30 seconds
  headers: {
    'Content-Type': 'application/json'
  },
  mode: process.env.REACT_APP_API_MODE || 'fake' // 'fake' or 'real'
};

// Log resolved API URL for debugging
console.info('API base URL:', ApiConfigurations.baseUrl);


//--## Table Functions and Variables

export function getMatchesCountText(count) {
  return count === 1 ? `1 match` : `${count} matches`;
}

export function formatDate(date) {
  const dateFormatter = new Intl.DateTimeFormat('en-US', { dateStyle: 'long' });
  const timeFormatter = new Intl.DateTimeFormat('en-US', { timeStyle: 'short', hour12: false });
  return `${dateFormatter.format(date)}, ${timeFormatter.format(date)}`;
}

export function createLabelFunction(columnName) {
  return ({ sorted, descending }) => {
    const sortState = sorted ? `sorted ${descending ? 'descending' : 'ascending'}` : 'not sorted';
    return `${columnName}, ${sortState}.`;
  };
}

export const paginationLabels = {
  nextPageLabel: 'Next page',
  pageLabel: pageNumber => `Go to page ${pageNumber}`,
  previousPageLabel: 'Previous page',
};

export const pageSizePreference = {
  title: 'Select page size',
  options: [
    { value: 10, label: '10 resources' },
    { value: 20, label: '20 resources' },
  ],
};

export function EmptyState({ title, subtitle, action }) {
  return (
    <Box textAlign="center" color="inherit">
      <Box variant="strong" textAlign="center" color="inherit">
        {title}
      </Box>
      <Box variant="p" padding={{ bottom: 's' }} color="inherit">
        {subtitle}
      </Box>
      {action}
    </Box>
  );
}
