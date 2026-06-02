import React from "react";
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AppLayoutToolbar,
  BreadcrumbGroup,
  SpaceBetween,
  Container,
  Box,
  Button,
  Header,
  Grid,
  Icon,
  ExpandableSection,
  SideNavigation,
} from "@cloudscape-design/components";
import { SideNavigationConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";

// Feature card component for the grid
function FeatureCard({ iconName, title, description, iconColor }) {
  return (
    <Container>
      <Box
        padding={{
          vertical: "l",
          horizontal: "l",
        }}
      >
        <SpaceBetween size="m">
          <Icon name={iconName} size="big" variant={iconColor || "subtle"} />
          <Box fontWeight="bold" fontSize="heading-m">
            {title}
          </Box>
          <Box color="text-body-secondary" variant="p">
            {description}
          </Box>
        </SpaceBetween>
      </Box>
    </Container>
  );
}

// Use case expandable section component
function UseCaseSection({ headerText, challenge, challengeLabel }) {
  return (
    <ExpandableSection
      headerText={headerText}
      variant="container"
     
    >
      <SpaceBetween size="s">
        <Box variant="awsui-key-label">{challengeLabel}</Box>
        <Box variant="p">{challenge}</Box>
      </SpaceBetween>
    </ExpandableSection>
  );
}

// Main landing page component
function DatabaseModernizerLanding() {
  const { t } = useTranslation();

  //-- Variable for Navigation Panel
  const [navigationOpen, setNavigationOpen] = useState(false);

  const features = [
    {
      iconName: "status-in-progress",
      iconColor: "link",
      title: t("landing.features.workload-analysis-title"),
      description: t("landing.features.workload-analysis-desc"),
    },
    {
      iconName: "suggestions",
      iconColor: "success",
      title: t("landing.features.target-recommendations-title"),
      description: t("landing.features.target-recommendations-desc"),
    },
    {
      iconName: "view-full",
      iconColor: "warning",
      title: t("landing.features.tco-title"),
      description: t("landing.features.tco-desc"),
    },
    {
      iconName: "gen-ai",
      iconColor: "link",
      title: t("landing.features.schema-design-title"),
      description: t("landing.features.schema-design-desc"),
    },
    {
      iconName: "script",
      iconColor: "normal",
      title: t("landing.features.migration-plans-title"),
      description: t("landing.features.migration-plans-desc"),
    },
    {
      iconName: "security",
      iconColor: "success",
      title: t("landing.features.aws-environment-title"),
      description: t("landing.features.aws-environment-desc"),
    },
    {
      iconName: "upload-download",
      iconColor: "link",
      title: t("landing.features.multi-db-title"),
      description: t("landing.features.multi-db-desc"),
    },
    {
      iconName: "view-horizontal",
      iconColor: "normal",
      title: t("landing.features.comparison-title"),
      description: t("landing.features.comparison-desc"),
    },
  ];
  const useCases = [
    {
      headerText: t("landing.use-cases.legacy-title"),
      challenge: t("landing.use-cases.legacy-challenge"),
    },
    {
      headerText: t("landing.use-cases.cost-optimization-title"),
      challenge: t("landing.use-cases.cost-optimization-challenge"),
    },
    {
      headerText: t("landing.use-cases.purpose-built-title"),
      challenge: t("landing.use-cases.purpose-built-challenge"),
    },
    {
      headerText: t("landing.use-cases.migration-planning-title"),
      challenge: t("landing.use-cases.migration-planning-challenge"),
    },
    {
      headerText: t("landing.use-cases.ma-integration-title"),
      challenge: t("landing.use-cases.ma-integration-challenge"),
    },
    {
      headerText: t("landing.use-cases.fleet-assessment-title"),
      challenge: t("landing.use-cases.fleet-assessment-challenge"),
    },
  ];
  return (
    <>
      <AppHeader />
      <AppLayoutToolbar
      content={
        <SpaceBetween size="l">
          {/* Hero Section */}
          <Box
            padding={{
              vertical: "xxl",
              horizontal: "l",
            }}
          >
            <SpaceBetween size="l">
              <Grid
                gridDefinition={[
                  {
                    colspan: {
                      default: 12,
                      l: 8,
                      m: 12,
                      s: 12,
                    },
                  },
                  {
                    colspan: {
                      default: 12,
                      l: 4,
                      m: 12,
                      s: 12,
                    },
                  },
                ]}
              >
                <SpaceBetween size="m">
                  <Box
                    fontSize="display-l"
                    fontWeight="bold"
                  >
                    {t("landing.hero.title")}
                  </Box>
                  <Box
                    color="text-body-secondary"
                    fontSize="heading-m"
                    fontWeight="light"
                  >
                    {t("landing.hero.subtitle")}
                  </Box>
                </SpaceBetween>

                <Box
                  padding={{
                    top: "l",
                  }}
                >
                  <Container>
                    <Box
                      padding={{
                        vertical: "m",
                        horizontal: "m",
                      }}
                    >
                      <SpaceBetween size="m">
                        <Box>
                          <SpaceBetween size="xs">
                            <Box fontWeight="bold" fontSize="heading-s">
                              {t("landing.hero.get-started-title")}
                            </Box>
                            <Box color="text-body-secondary" variant="small">
                              {t("landing.hero.get-started-desc")}
                            </Box>
                          </SpaceBetween>
                        </Box>
                        <Button
                          iconName="add-plus"
                          href="/analysis/create"
                          variant="primary"
                          fullWidth
                        >
                          {t("landing.hero.start-new-analysis")}
                        </Button>
                        <Button href="/dashboard" variant="normal" fullWidth>
                          {t("landing.hero.view-dashboard")}
                        </Button>
                      </SpaceBetween>
                    </Box>
                  </Container>
                </Box>
              </Grid>
            </SpaceBetween>
          </Box>

          {/* Key Features Section */}
          <Box
            padding={{
              horizontal: "l",
            }}
          >
            <SpaceBetween size="l">
              <Box textAlign="center">
                <SpaceBetween size="xs">
                  <Box fontSize="heading-xl" fontWeight="bold">
                    {t("landing.features.title")}
                  </Box>
                  <Box color="text-body-secondary" fontSize="heading-s">
                    {t("landing.features.subtitle")}
                  </Box>
                </SpaceBetween>
              </Box>

              <Grid
                gridDefinition={[
                  {
                    colspan: {
                      default: 12,
                      l: 6,
                      m: 6,
                      s: 12,
                    },
                  },
                  {
                    colspan: {
                      default: 12,
                      l: 6,
                      m: 6,
                      s: 12,
                    },
                  },
                  {
                    colspan: {
                      default: 12,
                      l: 6,
                      m: 6,
                      s: 12,
                    },
                  },
                  {
                    colspan: {
                      default: 12,
                      l: 6,
                      m: 6,
                      s: 12,
                    },
                  },
                  {
                    colspan: {
                      default: 12,
                      l: 6,
                      m: 6,
                      s: 12,
                    },
                  },
                  {
                    colspan: {
                      default: 12,
                      l: 6,
                      m: 6,
                      s: 12,
                    },
                  },
                  {
                    colspan: {
                      default: 12,
                      l: 6,
                      m: 6,
                      s: 12,
                    },
                  },
                  {
                    colspan: {
                      default: 12,
                      l: 6,
                      m: 6,
                      s: 12,
                    },
                  },
                ]}
              >
                {features.map((feature, index) => (
                  <FeatureCard
                    key={index}
                    iconName={feature.iconName}
                    iconColor={feature.iconColor}
                    title={feature.title}
                    description={feature.description}
                  />
                ))}
              </Grid>
            </SpaceBetween>
          </Box>

          {/* Use Cases Section */}
          <Container
            header={
              <Header
                description={t("landing.use-cases.description")}
                variant="h2"
              >
                {t("landing.use-cases.title")}
              </Header>
            }
           
          >
            <SpaceBetween size="l">
              {useCases.map((useCase, index) => (
                <UseCaseSection
                  key={index}
                  headerText={useCase.headerText}
                  challenge={useCase.challenge}
                  challengeLabel={t("landing.use-cases.the-challenge")}
                />
              ))}
            </SpaceBetween>
          </Container>
        </SpaceBetween>
      }
      contentType="default"
      navigation={
        <SideNavigation
          activeHref="/"
          header={SideNavigationConfigurations.header}
          items={SideNavigationConfigurations.items}
        />
      }
      toolsHide
      navigationOpen={navigationOpen}
      onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
    />
    </>
  );
}
export default DatabaseModernizerLanding;
