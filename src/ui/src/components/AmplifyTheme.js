import { createTheme } from "@aws-amplify/ui-react";

export const AmplifyTheme = createTheme({
  name: 'cloudscape-theme',
  tokens: {
    fontSizes: {
      small: { value: '14px' },
      medium: { value: '15px' },
      large: { value: '16px' }
    },
    colors: {
      brand: {
        primary: {
          10: { value: '#f1faff' },
          20: { value: '#d4f0ff' },
          40: { value: '#84cbff' },
          60: { value: '#16afff' },
          80: { value: 'rgb(66,180,255)' }, // CloudScape primary orange
          85: { value: '#0972d3' }, // CloudScape primary blue
          90: { value: '#033160' },
          100: { value: '#032b54' }
        }
      },
      background: {
        primary: { value: 'rgb(19,25,32)' },
        secondary: { value: 'rgba(255, 255, 255, 0.85)' } // Semi-transparent
      },
      font: {
        primary: { value: '#ffffff' },
        secondary: { value: '#e5e7eb' }
      },
      border: {
        primary: { value: '#d1d5db' }, // CloudScape border color
        secondary: { value: '#e5e7eb' },
        borderRadius: { value: '2px' }
      }
    },
    components: {
      authenticator: {
        fontSize: { value: '{fontSizes.small}' }
      },
      signin: {
        fontSize: { value: '{fontSizes.small}' }
      },
      forgotyourpassword: {
        fontSize: { value: '{fontSizes.small}' }
      },
      button: {
        primary: {
          backgroundColor: { value: '{colors.brand.primary.80}' },
          color: { value: '#000000' },
          borderRadius: { value: '2px' } // CloudScape uses squared buttons
        }
      },
      input: {
        color: { value: '#fff' }, // CloudScape text color
        borderColor: { value: '{colors.border.primary}' },
        borderRadius: { value: '2px' }
      },
      label: {
        fontSize: { value: '{fontSizes.small}' }
      },
      text: {
        fontSize: { value: '{fontSizes.small}' }
      },
      message: {
        fontSize: { value: '{fontSizes.small}' }
      },
      alert: {
        fontSize: { value: '{fontSizes.small}' }
      },
      fieldcontrol: {
        fontSize: { value: '{fontSizes.small}' }
      }
    }
  }
});
